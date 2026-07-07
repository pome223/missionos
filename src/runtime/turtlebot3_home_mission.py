"""TurtleBot3 home-robot mission wrapper for bounded Nav2 simulator control.

This module keeps the home-robot story at mission level. TurtleBot3 can prove a
bounded Nav2 move in simulation when the external ROS2 bridge reports both Nav2
completion and odom motion. It cannot prove cleaning, payload pickup, payload
dropoff, whole-home coverage, or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.intelligence.turtlebot3_recovery_planner import (
    TURTLEBOT3_RECOVERY_PLANNER_RESULT_SCHEMA_VERSION,
    run_turtlebot3_recovery_planner,
)
from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.mission_episode_review import (
    build_mission_episode_review,
    mission_episode_review_ref,
)
from src.runtime.mission_autonomy_envelope import (
    MissionAutonomyRecoveryProposal,
    approve_mission_autonomy_envelope,
    build_mission_autonomy_envelope,
    build_mission_autonomy_recovery_proposal,
    classify_mission_autonomy_recovery_proposal,
)
from src.runtime.nvblox_perception_evidence import (
    build_nvblox_perception_evidence_from_env_or_responses,
)
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
    Ros2Nav2BridgeCommandClient,
    Ros2Nav2BridgeError,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
    build_blocked_ros2_nav2_hardware_adapter_evidence,
)
from src.runtime.turtlebot3_log_collector import (
    TurtleBot3LogCollectorError,
    build_turtlebot3_nav2_log_diagnostics,
    collect_turtlebot3_log_bundle_from_env,
    turtlebot3_log_bundle_ref_from_env,
)
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV,
    TurtleBot3TelemetrySidecarError,
    build_turtlebot3_state_correlation,
    build_turtlebot3_telemetry_window_from_jsonl,
)


TURTLEBOT3_HOME_MISSION_PLAN_SCHEMA = "missionos_turtlebot3_home_mission_plan.v1"
TURTLEBOT3_HOME_MISSION_APPROVAL_SCHEMA = (
    "missionos_turtlebot3_home_mission_approval.v1"
)
TURTLEBOT3_HOME_MISSION_EXECUTION_SCHEMA = (
    "missionos_turtlebot3_home_mission_execution.v1"
)
TURTLEBOT3_INDOOR_MAP_MODEL_SCHEMA = "missionos_turtlebot3_indoor_map_model.v1"

TurtleBotNav2RobotProfile = Literal["turtlebot3", "turtlebot4", "nova_carter"]
TurtleBotNav2ExecutionTarget = Literal[
    "ros2_nav2_turtlebot3_sim",
    "ros2_nav2_turtlebot4_sim",
    "isaac_ros_nav2_nova_carter_sim",
]
TurtleBot3HomeMissionKind = Literal[
    "indoor_patrol_leg",
    "indoor_delivery_route_leg",
    "obstacle_avoidance_patrol_leg",
    "cleaning_inspection_leg",
    "payload_transport_rehearsal_leg",
    "bounded_go_to_waypoint",
]
TurtleBot3JudgmentKind = Literal["battery_envelope", "obstacle_avoidance"]
TurtleBot3JudgmentDecision = Literal["allow", "block", "observe_required"]

_TRUE_VALUES = {"1", "true", "yes", "on"}
TURTLEBOT_HOME_ROBOT_PROFILE_ENV = "MISSIONOS_TURTLEBOT_HOME_ROBOT_PROFILE"
_TURTLEBOT_NAV2_PROFILE_SPECS = {
    "turtlebot3": {
        "robot_label": "TurtleBot3",
        "robot_model": "turtlebot3_burger",
        "execution_target": "ros2_nav2_turtlebot3_sim",
        "runtime_substrate": "Gazebo Classic + ROS2/Nav2",
        "runtime_profile": "turtlebot3_gazebo_nav2",
        "bridge_not_configured_reason": "ros2_nav2_bridge_command_missing",
        "runtime_not_enabled_reason": (
            f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV}_not_enabled"
        ),
        "map_name": "TurtleBot3 indoor Nav2 simulator map",
    },
    "turtlebot4": {
        "robot_label": "TurtleBot4",
        "robot_model": "turtlebot4_lite",
        "execution_target": "ros2_nav2_turtlebot4_sim",
        "runtime_substrate": "Gazebo Classic + ROS2/Nav2",
        "runtime_profile": "turtlebot4_gazebo_nav2",
        "bridge_not_configured_reason": "ros2_nav2_bridge_command_missing",
        "runtime_not_enabled_reason": (
            f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV}_not_enabled"
        ),
        "map_name": "TurtleBot4 indoor Nav2 simulator map",
    },
    "nova_carter": {
        "robot_label": "Nova Carter",
        "robot_model": "nova_carter",
        "execution_target": "isaac_ros_nav2_nova_carter_sim",
        "runtime_substrate": "NVIDIA Isaac Sim + Isaac ROS/Nav2",
        "runtime_profile": "nvidia_isaac_sim_nova_carter_nav2",
        "bridge_not_configured_reason": (
            "isaac_sim_nova_carter_bridge_command_missing"
        ),
        "runtime_not_enabled_reason": "isaac_sim_nova_carter_runtime_not_enabled",
        "map_name": "Nova Carter Isaac Sim Nav2 simulator map",
    },
}
_TURTLEBOT3_GOAL = Nav2GoalPose(
    frame_id="map",
    # Midpoint of the free corridor between the centre and table pillars
    # (0.55 m from each); the old x=0.75 left only 0.35 m to the pillar at
    # (1.1, 0.0), inside the inflated costmap.
    x_m=0.55,
    y_m=0.0,
    yaw_rad=0.0,
    tolerance_m=0.25,
    max_speed_mps=0.25,
    max_distance_m=3.0,
    label="turtlebot3_validated_home_patrol_leg",
)
_TURTLEBOT3_HOME_POSE = Nav2GoalPose(
    frame_id="map",
    x_m=-2.0,
    y_m=-0.5,
    yaw_rad=0.0,
    tolerance_m=0.25,
    max_speed_mps=0.25,
    max_distance_m=3.0,
    label="simulated_home_origin",
)
_TURTLEBOT3_DELIVERY_OBSTACLE_X_M = -1.15
_TURTLEBOT3_DELIVERY_OBSTACLE_Y_M = -0.5
_TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_X_M = 0.32
_TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_Y_M = 0.32
_TURTLEBOT3_HOME_LAYOUT_OBSTACLES = (
    {
        "name": "missionos_closed_door_blocker",
        "kind": "simulated_closed_door",
        "x_m": _TURTLEBOT3_DELIVERY_OBSTACLE_X_M,
        "y_m": _TURTLEBOT3_DELIVERY_OBSTACLE_Y_M,
        "size_x_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_X_M,
        "size_y_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_Y_M,
        "label": "closed door",
        "label_offset_y_px": -8,
    },
    {
        "name": "missionos_human_blocker",
        "kind": "simulated_human_blocker",
        "x_m": -1.00,
        "y_m": 0.55,
        "size_x_m": 0.24,
        "size_y_m": 0.24,
        "label": "person",
        "label_offset_y_px": 34,
    },
    {
        "name": "missionos_dog_blocker",
        "kind": "simulated_pet_blocker",
        "x_m": 0.70,
        "y_m": 0.55,
        "size_x_m": 0.16,
        "size_y_m": 0.16,
        "label": "dog",
        "label_offset_y_px": -8,
    },
)
# Wall polygon simplified from the turtlebot3_navigation2 SLAM occupancy grid
# (map.pgm, resolution 0.05 m, origin -10/-10); pillar coordinates and radius
# from the turtlebot3_world model.sdf collision entries. Rooms remain a
# narrative overlay, but they are tiled without overlap inside the real arena
# and each named furniture item sits on a real pillar footprint.
_TURTLEBOT3_ARENA_WALL_POLYGON = (
    {"x_m": 2.375, "y_m": 0.025},
    {"x_m": 2.625, "y_m": 0.475},
    {"x_m": 2.575, "y_m": 0.675},
    {"x_m": 1.875, "y_m": 1.875},
    {"x_m": 1.425, "y_m": 1.975},
    {"x_m": 1.175, "y_m": 2.425},
    {"x_m": 1.025, "y_m": 2.525},
    {"x_m": -0.925, "y_m": 2.525},
    {"x_m": -1.325, "y_m": 2.025},
    {"x_m": -1.775, "y_m": 1.975},
    {"x_m": -2.875, "y_m": -0.025},
    {"x_m": -1.775, "y_m": -1.925},
    {"x_m": -1.375, "y_m": -1.975},
    {"x_m": -1.025, "y_m": -2.525},
    {"x_m": 1.025, "y_m": -2.525},
    {"x_m": 1.375, "y_m": -2.025},
    {"x_m": 1.825, "y_m": -1.925},
    {"x_m": 2.575, "y_m": -0.675},
    {"x_m": 2.625, "y_m": -0.475},
)
_TURTLEBOT3_WORLD_PILLAR_RADIUS_M = 0.15
_TURTLEBOT3_WORLD_PILLARS = (
    {"x_m": -1.1, "y_m": -1.1, "furniture_label": "plant"},
    {"x_m": -1.1, "y_m": 0.0, "furniture_label": "cabinet"},
    {"x_m": -1.1, "y_m": 1.1, "furniture_label": "lamp"},
    {"x_m": 0.0, "y_m": -1.1, "furniture_label": "counter"},
    {"x_m": 0.0, "y_m": 0.0, "furniture_label": "column"},
    {"x_m": 0.0, "y_m": 1.1, "furniture_label": "bookshelf"},
    {"x_m": 1.1, "y_m": -1.1, "furniture_label": "stool"},
    {"x_m": 1.1, "y_m": 0.0, "furniture_label": "table"},
    {"x_m": 1.1, "y_m": 1.1, "furniture_label": "sofa"},
)


def _turtlebot3_world_pillar_records() -> list[dict[str, Any]]:
    return [
        {
            "name": f"turtlebot3_world_pillar_{index}",
            "kind": "turtlebot3_world_pillar",
            "x_m": pillar["x_m"],
            "y_m": pillar["y_m"],
            "radius_m": _TURTLEBOT3_WORLD_PILLAR_RADIUS_M,
            "furniture_label": pillar["furniture_label"],
            "sim_collision_spawned": True,
            "source": "turtlebot3_world_model_sdf_collision",
        }
        for index, pillar in enumerate(_TURTLEBOT3_WORLD_PILLARS, start=1)
    ]


def _turtlebot3_world_furniture_records() -> list[dict[str, Any]]:
    narrative = {"sofa": "sofa", "table": "table", "bookshelf": "bookshelf", "counter": "counter"}
    records: list[dict[str, Any]] = []
    for pillar in _TURTLEBOT3_WORLD_PILLARS:
        label = pillar["furniture_label"]
        if label not in narrative:
            continue
        diameter = _TURTLEBOT3_WORLD_PILLAR_RADIUS_M * 2.0
        records.append(
            {
                "name": f"missionos_{label}",
                "kind": f"simulated_furniture_{label}",
                "label": label,
                "x_m": pillar["x_m"],
                "y_m": pillar["y_m"],
                "size_x_m": diameter,
                "size_y_m": diameter,
                "sim_collision_spawned": True,
                "source": "turtlebot3_world_model_sdf_collision",
            }
        )
    order = ["sofa", "table", "bookshelf", "counter"]
    return sorted(records, key=lambda record: order.index(record["label"]))


_TURTLEBOT3_HOME_FLOOR_PLAN = {
    "schema_version": "missionos_turtlebot3_simulated_home_floor_plan.v2",
    "floor_plan_id": "turtlebot3_simulated_home_loop.v1",
    "source": "turtlebot3_world_sdf_and_nav2_slam_map",
    "geometry_sources": {
        "wall_polygon": (
            "turtlebot3_navigation2 map.pgm occupancy grid, inner wall "
            "boundary simplified (RDP, 0.07 m)"
        ),
        "pillars": "turtlebot3_world model.sdf collision cylinders",
        "rooms": "missionos_narrative_overlay_display_only",
    },
    "bounds": {
        "min_x_m": -2.95,
        "max_x_m": 2.70,
        "min_y_m": -2.60,
        "max_y_m": 2.60,
    },
    "wall_polygon": [dict(point) for point in _TURTLEBOT3_ARENA_WALL_POLYGON],
    "pillars": _turtlebot3_world_pillar_records(),
    "rooms": [
        {
            "room_id": "entry",
            "label": "Entry",
            "min_x_m": -2.45,
            "max_x_m": -1.45,
            "min_y_m": -1.05,
            "max_y_m": 0.05,
        },
        {
            "room_id": "lower_corridor",
            "label": "Lower corridor",
            "min_x_m": -1.45,
            "max_x_m": -0.30,
            "min_y_m": -1.35,
            "max_y_m": -0.55,
        },
        {
            "room_id": "kitchen",
            "label": "Kitchen",
            "min_x_m": -0.30,
            "max_x_m": 0.85,
            "min_y_m": -1.35,
            "max_y_m": -0.25,
        },
        {
            "room_id": "dining",
            "label": "Dining",
            "min_x_m": 0.45,
            "max_x_m": 1.75,
            "min_y_m": -0.25,
            "max_y_m": 0.30,
        },
        {
            "room_id": "living",
            "label": "Living",
            "min_x_m": 0.65,
            "max_x_m": 1.75,
            "min_y_m": 0.30,
            "max_y_m": 1.45,
        },
        {
            "room_id": "study",
            "label": "Bookshelf aisle",
            "min_x_m": -0.45,
            "max_x_m": 0.65,
            "min_y_m": 0.30,
            "max_y_m": 1.45,
        },
        {
            "room_id": "side_hall",
            "label": "Side hall",
            "min_x_m": -1.45,
            "max_x_m": -0.45,
            "min_y_m": -0.15,
            "max_y_m": 0.85,
        },
    ],
    "furniture": _turtlebot3_world_furniture_records(),
    "claim_boundary": (
        "Walls and pillars are sourced from the TurtleBot3 world SDF and the "
        "Nav2 SLAM map of that world. Rooms are a narrative display overlay; "
        "furniture labels name real pillar collision footprints."
    ),
    "physical_execution_invoked": False,
    "mission_delivery_completion_claimed": False,
}
_TURTLEBOT3_DYNAMIC_OBSTACLE_APPROACH_SEGMENT = Nav2GoalPose(
    frame_id="map",
    x_m=-1.15,
    y_m=-0.85,
    yaw_rad=0.0,
    tolerance_m=0.25,
    max_speed_mps=0.25,
    max_distance_m=3.0,
    label="simulated_dynamic_obstacle_approach",
)
_TURTLEBOT3_DYNAMIC_OBSTACLE_AVOIDANCE_GOAL = Nav2GoalPose(
    frame_id="map",
    x_m=-0.85,
    y_m=-0.85,
    yaw_rad=0.0,
    tolerance_m=0.25,
    max_speed_mps=0.25,
    max_distance_m=3.0,
    label="runtime_recovery_avoid_obstacle_waypoint",
)
_TURTLEBOT3_DELIVERY_ROUTE_SEGMENTS = (
    Nav2GoalPose(
        frame_id="map",
        x_m=-1.15,
        y_m=-0.85,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_obstacle_avoidance_waypoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=-0.35,
        y_m=-0.85,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_lower_corridor_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=0.35,
        y_m=-0.55,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_kitchen_entry_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        # 0.65 m from the nearest turtlebot3_world pillar; the old (0.95,
        # -0.15) sat 0.21 m from the table pillar at (1.1, 0.0), inside the
        # inflated costmap, which forced Nav2 recovery spins.
        x_m=0.75,
        y_m=-0.55,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_dining_table_bypass_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=1.15,
        y_m=0.45,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_living_room_turn_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=0.55,
        y_m=0.80,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_bookshelf_aisle_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=-0.20,
        y_m=0.65,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_alternate_door_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=-0.75,
        y_m=0.25,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_person_pet_detour_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        # Centered in the free cell between four pillars (0.71 m clearance);
        # the old (-0.15, -0.15) sat 0.21 m from the centre pillar at (0, 0).
        x_m=-0.55,
        y_m=-0.45,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_return_corridor_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        # Matches _TURTLEBOT3_GOAL: equidistant (0.55 m) from the centre and
        # table pillars instead of hugging the table pillar.
        x_m=0.55,
        y_m=0.0,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_dropoff_waypoint",
    ),
)

# --- turtlebot3_house world profile -----------------------------------------
# Opt-in via MISSIONOS_TURTLEBOT3_WORLD_PROFILE=house. The house route starts
# at the stock spawn in the front yard, passes the mailbox, enters through the
# real front-door opening in Wall_108 (gap x 0.66..1.56 at y=-0.17), follows
# the hallway west, and drops off beside the wooden table in the living room.
# All clearances are against wall/furniture collisions extracted from the
# house SDF (config/turtlebot3_house/floor_plan.json).
TURTLEBOT3_WORLD_PROFILE_ENV = "MISSIONOS_TURTLEBOT3_WORLD_PROFILE"
TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL"
)
TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF"
)
TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_ACTOR_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_ACTOR"
)
_TURTLEBOT3_HOUSE_FLOOR_PLAN_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "turtlebot3_house"
    / "floor_plan.json"
)


def _house_goal_pose(x_m: float, y_m: float, label: str) -> Nav2GoalPose:
    return Nav2GoalPose(
        frame_id="map",
        x_m=x_m,
        y_m=y_m,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        # The house operating volume covers the full floor plan (rooms out to
        # ~7.5 m from the map origin); the arena default of 3.0 m would block
        # far-room goals in adapter preflight as operating_volume_violation.
        max_distance_m=8.0,
        label=label,
    )


_TURTLEBOT3_HOUSE_HOME_POSE = _house_goal_pose(
    -2.0, -0.5, "simulated_house_front_yard_origin"
)
_TURTLEBOT3_HOUSE_ROUTE_SEGMENTS = (
    _house_goal_pose(-0.5, -0.8, "simulated_front_yard_checkpoint"),
    _house_goal_pose(1.15, -0.9, "simulated_mailbox_approach_checkpoint"),
    _house_goal_pose(1.11, 0.45, "simulated_front_door_passage_checkpoint"),
    _house_goal_pose(-0.9, 0.4, "simulated_hallway_checkpoint"),
    _house_goal_pose(-2.66, 1.5, "simulated_living_room_entry_checkpoint"),
    _house_goal_pose(-1.4, 2.42, "simulated_table_dropoff_waypoint"),
)
_TURTLEBOT3_HOUSE_ROUTE_PREFIX = _TURTLEBOT3_HOUSE_ROUTE_SEGMENTS[:4]
# Destination registry: branch waypoints continue from the hallway checkpoint
# and pass only through real door openings extracted from the house SDF.
_TURTLEBOT3_HOUSE_ROOM_DESTINATIONS = {
    "living": {
        "label": "Living room",
        "terms": ("living", "リビング", "居間"),
        "via": ((-2.66, 1.5, "simulated_living_room_entry_checkpoint"),),
        "dropoff": (-1.4, 2.42, "simulated_living_room_dropoff_waypoint"),
        "rooms": ("living room",),
    },
    "study": {
        "label": "Study",
        "terms": ("study", "書斎", "勉強部屋"),
        "via": (
            (-2.66, 1.5, "simulated_living_room_entry_checkpoint"),
            (-4.5, 3.3, "simulated_living_west_checkpoint"),
            (-5.7, 3.3, "simulated_study_door_checkpoint"),
        ),
        "dropoff": (-6.4, 3.0, "simulated_study_dropoff_waypoint"),
        "rooms": ("living room", "study door", "study"),
    },
    "bedroom": {
        "label": "Bedroom",
        "terms": ("bedroom", "bed room", "寝室", "ベッドルーム"),
        "via": (
            (-2.66, 1.5, "simulated_living_room_entry_checkpoint"),
            (-4.5, 3.3, "simulated_living_west_checkpoint"),
            (-5.7, 3.3, "simulated_study_door_checkpoint"),
            (-6.3, 1.6, "simulated_study_south_checkpoint"),
            (-6.3, 0.2, "simulated_bedroom_door_checkpoint"),
        ),
        "dropoff": (-6.4, -1.6, "simulated_bedroom_dropoff_waypoint"),
        "rooms": ("living room", "study", "bedroom door", "bedroom"),
    },
    "lounge": {
        "label": "Lounge",
        "terms": ("lounge", "ラウンジ", "応接"),
        "via": (
            (1.7, 0.4, "simulated_hallway_east_checkpoint"),
            (2.9, 0.4, "simulated_lounge_door_checkpoint"),
        ),
        "dropoff": (4.3, 1.5, "simulated_lounge_dropoff_waypoint"),
        "rooms": ("lounge door", "lounge"),
    },
    "dining": {
        "label": "Dining",
        "terms": ("dining", "ダイニング", "食堂"),
        "via": (
            (1.7, 0.4, "simulated_hallway_east_checkpoint"),
            (2.9, 0.4, "simulated_lounge_door_checkpoint"),
            (5.4, 0.35, "simulated_lounge_south_checkpoint"),
            (6.2, -0.8, "simulated_dining_door_checkpoint"),
        ),
        "dropoff": (5.7, -2.7, "simulated_dining_dropoff_waypoint"),
        "rooms": ("lounge", "dining door", "dining"),
    },
    "pantry": {
        "label": "Pantry",
        "terms": ("pantry", "パントリー", "納戸"),
        "via": (
            (1.7, 0.4, "simulated_hallway_east_checkpoint"),
            (2.9, 0.4, "simulated_lounge_door_checkpoint"),
            (3.1, 3.9, "simulated_lounge_north_checkpoint"),
            (2.3, 4.6, "simulated_pantry_door_checkpoint"),
        ),
        "dropoff": (1.0, 3.2, "simulated_pantry_dropoff_waypoint"),
        "rooms": ("lounge", "pantry door", "pantry"),
    },
}
_TURTLEBOT3_HOUSE_DEFAULT_DESTINATION = "living"


def house_destination_room_from_instruction(text: str) -> str:
    """Resolve a named destination room from the operator instruction."""

    lower = str(text or "").lower()
    for room_id, record in _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS.items():
        if any(term in lower for term in record["terms"]):
            return room_id
    return _TURTLEBOT3_HOUSE_DEFAULT_DESTINATION


def _house_route_segments_for_room(room_id: str) -> tuple[Nav2GoalPose, ...]:
    record = _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS.get(
        room_id, _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS[_TURTLEBOT3_HOUSE_DEFAULT_DESTINATION]
    )
    branch = [
        _house_goal_pose(x_m, y_m, label) for x_m, y_m, label in record["via"]
    ]
    dropoff = _house_goal_pose(*record["dropoff"])
    return (*_TURTLEBOT3_HOUSE_ROUTE_PREFIX, *branch, dropoff)


def _house_room_sequence_for_room(room_id: str) -> list[str]:
    record = _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS.get(
        room_id, _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS[_TURTLEBOT3_HOUSE_DEFAULT_DESTINATION]
    )
    return [
        "home",
        "front yard",
        "mailbox",
        "front door",
        "hallway",
        *record["rooms"],
        "dropoff",
    ]


_TURTLEBOT3_HOUSE_GOAL = _house_route_segments_for_room(
    _TURTLEBOT3_HOUSE_DEFAULT_DESTINATION
)[-1]
_TURTLEBOT3_HOUSE_DYNAMIC_OBSTACLE_APPROACH_SEGMENT = (
    _TURTLEBOT3_HOUSE_ROUTE_SEGMENTS[0]
)
_TURTLEBOT3_HOUSE_DYNAMIC_OBSTACLE_AVOIDANCE_GOAL = _house_goal_pose(
    -0.2, -1.4, "runtime_recovery_avoid_obstacle_waypoint"
)
# Mid-yard, 0.3 m south of the leg between the first two waypoints: forces a
# gentle avoidance arc in open space instead of a recovery scrum inside the
# mailbox/front-door funnel where AMCL has the least geometry to work with.
_TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_X_M = 0.2
_TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_Y_M = -1.2
_TURTLEBOT3_HOUSE_LAYOUT_OBSTACLES = (
    {
        "name": "missionos_closed_door_blocker",
        "kind": "simulated_closed_door",
        "x_m": _TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_X_M,
        "y_m": _TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_Y_M,
        "size_x_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_X_M,
        "size_y_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_Y_M,
        "label": "closed door",
        "label_offset_y_px": -8,
    },
    {
        "name": "missionos_human_blocker",
        "kind": "simulated_human_blocker",
        "x_m": -1.75,
        "y_m": 1.6,
        "size_x_m": 0.24,
        "size_y_m": 0.24,
        "label": "person",
        "label_offset_y_px": 34,
    },
    {
        "name": "missionos_dog_blocker",
        "kind": "simulated_pet_blocker",
        "x_m": -0.7,
        "y_m": 2.6,
        "size_x_m": 0.16,
        "size_y_m": 0.16,
        "label": "dog",
        "label_offset_y_px": -8,
    },
)
_TURTLEBOT3_HOUSE_ROOMS = (
    {"room_id": "front_yard", "label": "Front yard", "min_x_m": -5.15, "max_x_m": 4.9, "min_y_m": -2.6, "max_y_m": -0.17},
    {"room_id": "hallway", "label": "Hallway", "min_x_m": -5.15, "max_x_m": 2.3, "min_y_m": -0.17, "max_y_m": 0.93},
    {"room_id": "living", "label": "Living room", "min_x_m": -5.15, "max_x_m": -0.05, "min_y_m": 0.93, "max_y_m": 5.27},
    {"room_id": "pantry", "label": "Pantry", "min_x_m": -0.05, "max_x_m": 2.3, "min_y_m": 0.93, "max_y_m": 5.27},
    {"room_id": "lounge", "label": "Lounge", "min_x_m": 2.3, "max_x_m": 7.5, "min_y_m": -0.17, "max_y_m": 5.27},
    {"room_id": "study", "label": "Study", "min_x_m": -7.5, "max_x_m": -5.15, "min_y_m": 0.93, "max_y_m": 5.27},
    {"room_id": "bedroom", "label": "Bedroom", "min_x_m": -7.5, "max_x_m": -5.15, "min_y_m": -3.92, "max_y_m": 0.93},
    {"room_id": "dining", "label": "Dining", "min_x_m": 4.9, "max_x_m": 7.5, "min_y_m": -5.28, "max_y_m": -0.17},
)
_TURTLEBOT3_HOUSE_FURNITURE_LABELS = (
    ("bookshelf", "bookshelf"),
    ("cabinet", "cabinet"),
    ("cafe_table", "cafe table"),
    ("table_marble", "side table"),
    ("table", "table"),
    ("Mailbox", "mailbox"),
    ("first_2015_trash_can", "trash can"),
)


def _house_furniture_label(link_name: str) -> str | None:
    for prefix, label in _TURTLEBOT3_HOUSE_FURNITURE_LABELS:
        if link_name == prefix or link_name.startswith(f"{prefix}_"):
            if prefix == "table" and link_name.startswith("table_marble"):
                continue
            return label
    return None


def _load_house_floor_plan_source() -> dict[str, Any]:
    if not _TURTLEBOT3_HOUSE_FLOOR_PLAN_SOURCE.exists():
        raise ValueError(
            "turtlebot3_house floor plan source missing: "
            f"{_TURTLEBOT3_HOUSE_FLOOR_PLAN_SOURCE}; regenerate with "
            "scripts/turtlebot3_house_map_from_sdf.py"
        )
    return json.loads(_TURTLEBOT3_HOUSE_FLOOR_PLAN_SOURCE.read_text(encoding="utf-8"))


def _house_furniture_records(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in source.get("walls") or ():
        link = str(record.get("link") or "")
        label = _house_furniture_label(link)
        if label is None:
            continue
        half_x = float(record["size_x_m"]) / 2.0
        half_y = float(record["size_y_m"]) / 2.0
        yaw = float(record.get("yaw_rad") or 0.0)
        extent = abs(half_x * math.cos(yaw)) + abs(half_y * math.sin(yaw))
        extent_y = abs(half_x * math.sin(yaw)) + abs(half_y * math.cos(yaw))
        group = groups.setdefault(
            link,
            {
                "label": label,
                "min_x": math.inf,
                "max_x": -math.inf,
                "min_y": math.inf,
                "max_y": -math.inf,
            },
        )
        group["min_x"] = min(group["min_x"], float(record["x_m"]) - extent)
        group["max_x"] = max(group["max_x"], float(record["x_m"]) + extent)
        group["min_y"] = min(group["min_y"], float(record["y_m"]) - extent_y)
        group["max_y"] = max(group["max_y"], float(record["y_m"]) + extent_y)
    for record in source.get("cylinders") or ():
        link = str(record.get("link") or "")
        label = _house_furniture_label(link)
        if label is None:
            continue
        radius = float(record.get("radius_m") or 0.0)
        group = groups.setdefault(
            link,
            {
                "label": label,
                "min_x": math.inf,
                "max_x": -math.inf,
                "min_y": math.inf,
                "max_y": -math.inf,
            },
        )
        group["min_x"] = min(group["min_x"], float(record["x_m"]) - radius)
        group["max_x"] = max(group["max_x"], float(record["x_m"]) + radius)
        group["min_y"] = min(group["min_y"], float(record["y_m"]) - radius)
        group["max_y"] = max(group["max_y"], float(record["y_m"]) + radius)
    for record in source.get("mesh_markers") or ():
        link = str(record.get("link") or "")
        label = _house_furniture_label(link)
        if label is None:
            continue
        groups.setdefault(
            link,
            {
                "label": label,
                "min_x": float(record["x_m"]) - 0.2,
                "max_x": float(record["x_m"]) + 0.2,
                "min_y": float(record["y_m"]) - 0.2,
                "max_y": float(record["y_m"]) + 0.2,
            },
        )
    records = []
    minimum_display_size_m = 0.3
    for link, group in sorted(groups.items()):
        records.append(
            {
                "name": f"turtlebot3_house_{link}",
                "kind": "turtlebot3_house_furniture",
                "label": group["label"],
                "x_m": round((group["min_x"] + group["max_x"]) / 2.0, 4),
                "y_m": round((group["min_y"] + group["max_y"]) / 2.0, 4),
                "size_x_m": round(
                    max(group["max_x"] - group["min_x"], minimum_display_size_m), 4
                ),
                "size_y_m": round(
                    max(group["max_y"] - group["min_y"], minimum_display_size_m), 4
                ),
                "sim_collision_spawned": True,
                "source": "turtlebot3_house_model_sdf_collision",
            }
        )
    return records


def _house_floor_plan() -> dict[str, Any]:
    source = _load_house_floor_plan_source()
    walls = [
        {
            "x_m": record["x_m"],
            "y_m": record["y_m"],
            "yaw_rad": record.get("yaw_rad", 0.0),
            "size_x_m": record["size_x_m"],
            "size_y_m": record["size_y_m"],
        }
        for record in source.get("walls") or ()
        if str(record.get("link") or "").startswith("Wall_")
    ]
    return {
        "schema_version": "missionos_turtlebot3_simulated_home_floor_plan.v2",
        "floor_plan_id": "turtlebot3_house.v1",
        "source": "turtlebot3_house_model_sdf_collision",
        "geometry_sources": {
            "walls": (
                "turtlebot3_house model.sdf box collisions at robot lidar "
                "height (door lintels excluded)"
            ),
            "furniture": "turtlebot3_house model.sdf nested furniture models",
            "rooms": "missionos_narrative_overlay_display_only",
        },
        "bounds": dict(source.get("bounds") or {}),
        "wall_polygon": [],
        "walls": walls,
        "pillars": [],
        "rooms": [dict(room) for room in _TURTLEBOT3_HOUSE_ROOMS],
        "furniture": _house_furniture_records(source),
        "claim_boundary": (
            "Walls and furniture are sourced from the turtlebot3_house Gazebo "
            "SDF collisions. Rooms are a narrative display overlay."
        ),
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
    }


def _world_profile_name() -> str:
    """Resolve the active world profile; the house is the default experience.

    The pillar arena remains available for the historical regression corpus
    via MISSIONOS_TURTLEBOT3_WORLD_PROFILE=arena.
    """

    value = os.environ.get(TURTLEBOT3_WORLD_PROFILE_ENV, "").strip().lower()
    return value if value in {"arena", "house"} else "house"


def _profile_is_house() -> bool:
    return _world_profile_name() == "house"


def _profile_home_pose() -> Nav2GoalPose:
    return _TURTLEBOT3_HOUSE_HOME_POSE if _profile_is_house() else _TURTLEBOT3_HOME_POSE


def _profile_goal() -> Nav2GoalPose:
    return _TURTLEBOT3_HOUSE_GOAL if _profile_is_house() else _TURTLEBOT3_GOAL


def _profile_route_segments(
    destination_room: str | None = None,
) -> tuple[Nav2GoalPose, ...]:
    if _profile_is_house():
        return _house_route_segments_for_room(
            destination_room or _TURTLEBOT3_HOUSE_DEFAULT_DESTINATION
        )
    return _TURTLEBOT3_DELIVERY_ROUTE_SEGMENTS


def _profile_dynamic_obstacle_approach_segment() -> Nav2GoalPose:
    if _profile_is_house():
        return _TURTLEBOT3_HOUSE_DYNAMIC_OBSTACLE_APPROACH_SEGMENT
    return _TURTLEBOT3_DYNAMIC_OBSTACLE_APPROACH_SEGMENT


def _profile_dynamic_obstacle_avoidance_goal() -> Nav2GoalPose:
    if _profile_is_house():
        return _TURTLEBOT3_HOUSE_DYNAMIC_OBSTACLE_AVOIDANCE_GOAL
    return _TURTLEBOT3_DYNAMIC_OBSTACLE_AVOIDANCE_GOAL


def _profile_delivery_obstacle_xy() -> tuple[float, float]:
    if _profile_is_house():
        return (
            _TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_X_M,
            _TURTLEBOT3_HOUSE_DELIVERY_OBSTACLE_Y_M,
        )
    return (_TURTLEBOT3_DELIVERY_OBSTACLE_X_M, _TURTLEBOT3_DELIVERY_OBSTACLE_Y_M)


def _profile_layout_obstacles() -> tuple[dict[str, Any], ...]:
    if _profile_is_house():
        return _TURTLEBOT3_HOUSE_LAYOUT_OBSTACLES
    return _TURTLEBOT3_HOME_LAYOUT_OBSTACLES


def _profile_room_sequence(destination_room: str | None = None) -> list[str]:
    if _profile_is_house():
        return _house_room_sequence_for_room(
            destination_room or _TURTLEBOT3_HOUSE_DEFAULT_DESTINATION
        )
    return [
        "home",
        "lower corridor",
        "kitchen entry",
        "dining table bypass",
        "living room",
        "bookshelf aisle",
        "alternate door",
        "person/pet detour",
        "dropoff",
    ]


class TurtleBot3MissionJudgmentPoint(BaseModel):
    """Mission-level judge output; it is not dispatch authority by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str
    judgment_kind: TurtleBot3JudgmentKind
    judge_id: Literal["missionos_home_robot_safety_judge"] = (
        "missionos_home_robot_safety_judge"
    )
    decision: TurtleBot3JudgmentDecision
    dispatch_allowed: bool
    input_observations: dict[str, Any]
    required_runtime_observation: str | None = None
    blocking_reasons: tuple[str, ...] = ()
    claim_boundary: str
    llm_judgment_in_gate: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    progress_counted: Literal[False] = False


class TurtleBot3HomeMissionPlan(BaseModel):
    """Source-bound home mission proposal for bounded TurtleBot3/Nav2 route segments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_turtlebot3_home_mission_plan.v1"] = (
        TURTLEBOT3_HOME_MISSION_PLAN_SCHEMA
    )
    proposal_id: str
    mission_kind: TurtleBot3HomeMissionKind
    mission_objective: str
    operator_instruction: str
    robot_profile: TurtleBotNav2RobotProfile = "turtlebot3"
    robot_label: str = "TurtleBot3"
    robot_model: Literal[
        "turtlebot3_burger",
        "turtlebot4_lite",
        "nova_carter",
    ] = "turtlebot3_burger"
    execution_target: TurtleBotNav2ExecutionTarget = "ros2_nav2_turtlebot3_sim"
    runtime_substrate: str = "Gazebo Classic + ROS2/Nav2"
    runtime_profile: str = "turtlebot3_gazebo_nav2"
    execution_mode: Literal["sim"] = "sim"
    nav2_goal_pose: Nav2GoalPose = Field(default=_TURTLEBOT3_GOAL)
    planned_segments: tuple[Nav2GoalPose, ...] = Field(default=(_TURTLEBOT3_GOAL,))
    planned_route_distance_m: float = Field(default=0.0, ge=0.0)
    ai_judgment_points: tuple[TurtleBot3MissionJudgmentPoint, ...]
    obstacle_scenario: dict[str, Any]
    battery_envelope: dict[str, Any]
    home_distance_envelope: dict[str, Any]
    autonomy_envelope: dict[str, Any]
    recovery_planner_result: dict[str, Any] = Field(default_factory=dict)
    recovery_proposals: tuple[dict[str, Any], ...] = ()
    recovery_proposal_classifications: tuple[dict[str, Any], ...] = ()
    indoor_delivery_route: dict[str, Any]
    supported_claims: tuple[str, ...]
    blocked_claims: tuple[str, ...]
    safety_boundaries: tuple[str, ...]
    requires_operator_approval: Literal[True] = True
    dispatch_authority_created: Literal[False] = False
    progress_counted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _recovery_avoid_obstacle_requires_fresh_approval() -> bool:
    return _truthy_env(TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV)


def _stable_proposal_id(
    text: str,
    mission_kind: str,
    robot_profile: TurtleBotNav2RobotProfile,
) -> str:
    digest = hashlib.sha256(
        f"{robot_profile}\n{mission_kind}\n{text}".encode("utf-8")
    ).hexdigest()
    return f"{robot_profile}_home_{mission_kind}_{digest[:10]}"


def normalize_turtlebot_nav2_robot_profile(raw: Any) -> TurtleBotNav2RobotProfile:
    profile = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if profile in {"turtlebot4", "tb4"}:
        return "turtlebot4"
    if profile in {"nova_carter", "novacarter", "carter", "isaac_carter"}:
        return "nova_carter"
    return "turtlebot3"


def _normalize_home_robot_profile(raw: Any) -> TurtleBotNav2RobotProfile | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return normalize_turtlebot_nav2_robot_profile(value)


def infer_turtlebot_home_robot_profile(text: str) -> TurtleBotNav2RobotProfile:
    raw = str(text or "")
    lower = raw.lower()
    if any(
        token in lower
        for token in ("nova carter", "nova-carter", "nova_carter", "isaac carter")
    ):
        return "nova_carter"
    if any(token in lower for token in ("turtlebot4", "turtle bot 4", "tb4")):
        return "turtlebot4"
    if any(token in lower for token in ("turtlebot3", "turtle bot 3", "tb3")):
        return "turtlebot3"
    env_profile = _normalize_home_robot_profile(
        os.environ.get(TURTLEBOT_HOME_ROBOT_PROFILE_ENV)
    )
    return env_profile or "turtlebot3"


def _robot_profile_from_instruction(text: str) -> TurtleBotNav2RobotProfile:
    lower = str(text or "").lower()
    if any(
        term in lower
        for term in (
            "nova carter",
            "nova-carter",
            "nova_carter",
            "isaac carter",
            "isaac-carter",
            "nvidia carter",
        )
    ):
        return "nova_carter"
    if any(term in lower for term in ("turtlebot4", "turtle bot 4", "tb4")):
        return "turtlebot4"
    return "turtlebot3"


def _robot_profile_spec(profile: Any) -> dict[str, str]:
    return dict(_TURTLEBOT_NAV2_PROFILE_SPECS[normalize_turtlebot_nav2_robot_profile(profile)])


def _robot_profile_from_proposal(
    proposal: Mapping[str, Any],
) -> TurtleBotNav2RobotProfile:
    return normalize_turtlebot_nav2_robot_profile(proposal.get("robot_profile"))


def _robot_label(robot_profile: TurtleBotNav2RobotProfile) -> str:
    return _robot_profile_spec(robot_profile)["robot_label"]


def _robot_model(robot_profile: TurtleBotNav2RobotProfile) -> str:
    return _robot_profile_spec(robot_profile)["robot_model"]


def _execution_target(robot_profile: TurtleBotNav2RobotProfile) -> str:
    return _robot_profile_spec(robot_profile)["execution_target"]


def instruction_requests_turtlebot3_home_mission(text: str) -> bool:
    """Return True for home-robot/TurtleBot3 mission requests."""

    raw = str(text or "")
    lower = raw.lower()
    robot_terms = (
        "turtlebot3",
        "turtlebot4",
        "turtlebot",
        "tb3",
        "tb4",
        "nova carter",
        "nova-carter",
        "nova_carter",
        "isaac carter",
        "nvidia carter",
        "nav2",
        "亀",
        "亀さん",
        "タートルボット",
        "ロボット",
    )
    home_terms = ("家", "部屋", "室内", "屋内", "home", "room", "indoor")
    mission_terms = (
        "一周",
        "巡回",
        "見回",
        "掃除",
        "清掃",
        "荷物",
        "運ん",
        "届け",
        "配送",
        "障害物",
        "回避",
        "避け",
        "バッテリー",
        "電池",
        "足りない",
        "不足",
        "移動",
        "ルート",
        "走って",
        "avoid",
        "obstacle",
        "battery",
        "low battery",
        "move",
        "go to",
        "patrol",
        "inspect",
        "clean",
        "carry",
        "payload",
        "deliver",
    )
    has_robot = any(term in raw for term in robot_terms) or any(
        term in lower for term in robot_terms
    )
    has_home = any(term in raw for term in home_terms) or any(
        term in lower for term in home_terms
    )
    has_mission = any(term in raw for term in mission_terms) or any(
        term in lower for term in mission_terms
    )
    return has_mission and (has_robot or has_home)


def infer_turtlebot3_home_mission_kind(text: str) -> TurtleBot3HomeMissionKind:
    raw = str(text or "")
    lower = raw.lower()
    if any(token in raw for token in ("配送", "届け")) or any(
        token in lower for token in ("deliver", "dropoff", "drop-off")
    ):
        return "indoor_delivery_route_leg"
    if _instruction_requests_obstacle_challenge(raw):
        return "obstacle_avoidance_patrol_leg"
    if any(token in raw for token in ("掃除", "清掃")) or any(
        token in lower for token in ("clean", "vacuum")
    ):
        return "cleaning_inspection_leg"
    if any(token in raw for token in ("荷物", "運ん", "届け")) or any(
        token in lower for token in ("carry", "payload", "deliver")
    ):
        return "payload_transport_rehearsal_leg"
    if any(token in raw for token in ("一周", "巡回", "見回")) or any(
        token in lower for token in ("patrol", "inspect", "loop")
    ):
        return "indoor_patrol_leg"
    return "bounded_go_to_waypoint"


def _mission_objective(
    mission_kind: TurtleBot3HomeMissionKind,
    *,
    robot_label: str = "TurtleBot3",
) -> str:
    label = str(robot_label or "TurtleBot3")
    if mission_kind == "indoor_delivery_route_leg":
        return f"{label} indoor delivery route to a simulated dropoff via bounded Nav2 simulation"
    if mission_kind == "obstacle_avoidance_patrol_leg":
        return f"{label} obstacle-avoidance patrol leg via bounded Nav2 simulation"
    if mission_kind == "cleaning_inspection_leg":
        return f"{label} cleaning-inspection leg via bounded Nav2 simulation"
    if mission_kind == "payload_transport_rehearsal_leg":
        return f"{label} payload-transport route rehearsal via bounded Nav2 simulation"
    if mission_kind == "bounded_go_to_waypoint":
        return f"{label} bounded waypoint move via Nav2 simulation"
    return f"{label} indoor patrol leg via bounded Nav2 simulation"


def _blocked_claims(mission_kind: TurtleBot3HomeMissionKind) -> tuple[str, ...]:
    claims = [
        "physical_execution",
        "whole_home_loop_completion",
        "cleaning_completion",
        "payload_pickup",
        "payload_delivery_completion",
        "mission_delivery_completion",
    ]
    if mission_kind == "cleaning_inspection_leg":
        claims.extend(("vacuum_or_brush_actuator_invoked", "surface_cleanliness_verified"))
    if mission_kind == "payload_transport_rehearsal_leg":
        claims.extend(("gripper_or_payload_bay_invoked", "load_sensor_verified"))
    if mission_kind == "indoor_delivery_route_leg":
        claims.extend(("payload_loaded", "payload_handoff_verified"))
    if mission_kind == "obstacle_avoidance_patrol_leg":
        claims.append("obstacle_spawned_by_missionos")
    return tuple(dict.fromkeys(claims))


def _instruction_requests_obstacle_challenge(text: str) -> bool:
    raw = str(text or "")
    lower = raw.lower()
    return any(token in raw for token in ("障害物", "回避", "避け")) or any(
        token in lower for token in ("obstacle", "avoid")
    )


def _instruction_requests_low_battery_case(text: str) -> bool:
    raw = str(text or "")
    lower = raw.lower()
    return any(token in raw for token in ("バッテリー", "電池", "足りない", "不足")) or any(
        token in lower for token in ("battery", "low battery", "insufficient")
    )


def _instruction_requests_mid_mission_battery_recovery(text: str) -> bool:
    raw = str(text or "")
    lower = raw.lower()
    mid_tokens = ("途中", "走行中", "配送中", "巡回中", "mid-mission", "during")
    return _instruction_requests_low_battery_case(raw) and (
        any(token in raw for token in mid_tokens)
        or any(token in lower for token in mid_tokens)
    )


def _instruction_requests_mid_mission_obstacle_recovery(text: str) -> bool:
    raw = str(text or "")
    lower = raw.lower()
    mid_tokens = (
        "途中",
        "走行中",
        "配送中",
        "巡回中",
        "出たら",
        "出現",
        "現れ",
        "mid-mission",
        "during",
        "appears",
        "encounter",
    )
    return _instruction_requests_obstacle_challenge(raw) and (
        any(token in raw for token in mid_tokens)
        or any(token in lower for token in mid_tokens)
    )


def _battery_percent_from_instruction(text: str) -> float:
    import re

    raw = str(text or "")
    lower = raw.lower()
    match = re.search(r"(?P<value>\d+(?:\.\d+)?)\s*(?:%|パーセント)", raw)
    if match:
        return float(match.group("value"))
    if _instruction_requests_mid_mission_battery_recovery(raw):
        return 60.0
    if any(token in raw for token in ("足りない", "不足")) or any(
        token in lower for token in ("low battery", "insufficient")
    ):
        return 18.0
    return 100.0


def _build_battery_envelope(
    text: str,
    *,
    planned_route_distance_m: float | None = None,
) -> dict[str, Any]:
    mid_mission_recovery = _instruction_requests_mid_mission_battery_recovery(text)
    start_pct = _battery_percent_from_instruction(text)
    planned_distance = float(planned_route_distance_m or 0.0)
    estimated_consumption_pct = round(max(8.0, planned_distance * 2.0), 3)
    reserve_pct = 20.0
    minimum_required_pct = estimated_consumption_pct + reserve_pct
    dispatch_allowed = start_pct >= minimum_required_pct
    runtime_recovery_battery_pct = 18.0 if mid_mission_recovery else None
    runtime_recovery_required = (
        mid_mission_recovery
        and runtime_recovery_battery_pct is not None
        and runtime_recovery_battery_pct < minimum_required_pct
    )
    return {
        "schema_version": "missionos_turtlebot3_battery_envelope.v1",
        "battery_state_source": "operator_instruction_or_default",
        "battery_start_pct": start_pct,
        "planned_route_distance_m": round(planned_distance, 3),
        "estimated_consumption_pct": estimated_consumption_pct,
        "estimated_consumption_source": "planned_route_distance_projection",
        "reserve_pct": reserve_pct,
        "minimum_required_pct": minimum_required_pct,
        "dispatch_allowed": dispatch_allowed,
        "runtime_recovery_trigger_after_segment_index": 1
        if runtime_recovery_required
        else None,
        "runtime_recovery_battery_pct": runtime_recovery_battery_pct,
        "runtime_recovery_required": runtime_recovery_required,
        "blocking_reasons": []
        if dispatch_allowed
        else ["battery_below_minimum_required"],
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _home_distance_from_instruction(text: str) -> float | None:
    import re

    raw = str(text or "")
    patterns = (
        r"(?:home|ホーム|帰還|自宅|充電基地|ドック)[^\d]{0,12}(?P<value>\d+(?:\.\d+)?)\s*m",
        r"(?P<value>\d+(?:\.\d+)?)\s*m[^\d]{0,12}(?:from home|from dock|ホーム|帰還|自宅|ドック)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return float(match.group("value"))
    return None


def _build_home_distance_envelope(text: str, goal: Nav2GoalPose) -> dict[str, Any]:
    instruction_distance = _home_distance_from_instruction(text)
    if instruction_distance is not None:
        distance_to_home_m = instruction_distance
        distance_source = "operator_instruction"
        runtime_observed = False
        projected_from_planned_goal = False
    else:
        distance_to_home_m = round(
            math.hypot(
                goal.x_m - _profile_home_pose().x_m,
                goal.y_m - _profile_home_pose().y_m,
            ),
            3,
        )
        distance_source = "planned_nav2_goal_projection"
        runtime_observed = False
        projected_from_planned_goal = True
    projected_return_battery_required_pct = round(max(1.0, distance_to_home_m * 2.0), 3)
    return {
        "schema_version": "missionos_turtlebot3_home_distance_envelope.v1",
        "home_pose_ref": "map:simulated_home_origin",
        "robot_pose_ref": (
            "operator_instruction_estimate"
            if instruction_distance is not None
            else "planned_nav2_goal_pose"
        ),
        "distance_to_home_m": distance_to_home_m,
        "distance_to_home_source": distance_source,
        "distance_to_home_source_backed": True,
        "runtime_observed": runtime_observed,
        "projected_from_planned_goal": projected_from_planned_goal,
        "projected_return_battery_required_pct": projected_return_battery_required_pct,
        "projected_return_reserve_pct": 5.0,
        "claim_boundary": (
            "This is the only source-backed home-distance input available to "
            "the TurtleBot3 recovery planner in this slice. Runtime odom "
            "distance must be added as a separate observation before it can be "
            "claimed as runtime telemetry."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _build_autonomy_battery_policy() -> dict[str, Any]:
    return {
        "schema_version": "missionos_turtlebot3_autonomy_battery_policy.v1",
        "continue_delivery_min_margin_pct": 8.0,
        "return_home_min_margin_pct": 5.0,
        "critical_hold_pct": 10.0,
        "distance_to_home_m_source": "runtime_telemetry_when_available",
        "claim_boundary": (
            "Battery and home-distance observations may support a recovery "
            "proposal. They do not create dispatch authority by themselves."
        ),
    }


def _build_autonomy_emergency_harness() -> dict[str, Any]:
    return {
        "schema_version": "missionos_turtlebot3_emergency_harness.v1",
        "enabled": True,
        "bypass_llm_only_for": (
            "imminent_collision",
            "critical_battery_hold",
            "operator_estop",
        ),
        "allowed_harness_actions": ("hold", "safe_stop"),
        "requires_recorded_skip_reason": True,
        "claim_boundary": (
            "The emergency harness may stop motion, but it must record why "
            "LLM recovery proposal generation was skipped."
        ),
    }


def _build_autonomy_envelope(
    *,
    proposal_id: str,
    operator_approved: bool = False,
    operator_approval_ref: str | None = None,
) -> dict[str, Any]:
    preapproved_recovery_actions = ("return_home", "hold", "avoid_obstacle")
    requires_human_approval_for = ("reroute", "safe_stop", "ask_human")
    if _recovery_avoid_obstacle_requires_fresh_approval():
        preapproved_recovery_actions = ("return_home", "hold")
        requires_human_approval_for = (
            "avoid_obstacle",
            "reroute",
            "safe_stop",
            "ask_human",
        )
    return build_mission_autonomy_envelope(
        mission_ref=proposal_id,
        operator_approved=operator_approved,
        operator_approval_ref=operator_approval_ref,
        battery_policy=_build_autonomy_battery_policy(),
        emergency_harness=_build_autonomy_emergency_harness(),
        preapproved_recovery_actions=preapproved_recovery_actions,
        requires_human_approval_for=requires_human_approval_for,
        blocked_actions=(
            "raw_velocity",
            "unbounded_move",
            "physical_execution",
            "payload_delivery_completion",
        ),
    ).model_dump(mode="json")


def _planner_not_required_result() -> dict[str, Any]:
    return {
        "schema_version": TURTLEBOT3_RECOVERY_PLANNER_RESULT_SCHEMA_VERSION,
        "planner_status": "not_required",
        "blocking_reasons": [],
        "proposal": {},
        "guardrail": {},
        "llm_invocation_evidence": {},
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _deterministic_return_home_recovery_proposal(
    *,
    proposal_id: str,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
) -> MissionAutonomyRecoveryProposal:
    return build_mission_autonomy_recovery_proposal(
        mission_ref=proposal_id,
        proposal_source="deterministic_fallback",
        selected_action="return_home",
        reason=(
            "Battery envelope is below the requested mission reserve; propose "
            "return_home instead of dispatching the requested leg."
        ),
        input_observations={
            "battery_start_pct": battery_envelope.get("battery_start_pct"),
            "planned_route_distance_m": battery_envelope.get(
                "planned_route_distance_m"
            ),
            "estimated_consumption_pct": battery_envelope.get(
                "estimated_consumption_pct"
            ),
            "minimum_required_pct": battery_envelope.get("minimum_required_pct"),
            "distance_to_home_m": home_distance_envelope.get("distance_to_home_m"),
            "distance_to_home_source": home_distance_envelope.get(
                "distance_to_home_source"
            ),
            "projected_return_battery_required_pct": home_distance_envelope.get(
                "projected_return_battery_required_pct"
            ),
        },
    )


def _deterministic_avoid_obstacle_recovery_proposal(
    *,
    proposal_id: str,
    obstacle_scenario: Mapping[str, Any],
) -> MissionAutonomyRecoveryProposal:
    return build_mission_autonomy_recovery_proposal(
        mission_ref=proposal_id,
        proposal_source="deterministic_fallback",
        selected_action="avoid_obstacle",
        reason=(
            "Runtime obstacle evidence is source-backed; propose a bounded "
            "Nav2 avoid_obstacle waypoint before resuming the delivery route."
        ),
        input_observations={
            "runtime_obstacle_observed": obstacle_scenario.get(
                "runtime_obstacle_observed"
            ),
            "costmap_obstacle_observed": obstacle_scenario.get(
                "costmap_obstacle_observed"
            ),
            "runtime_obstacle_source": obstacle_scenario.get(
                "runtime_obstacle_source"
            ),
            "recommended_recovery_action": obstacle_scenario.get(
                "recommended_recovery_action"
            ),
            "recommended_avoidance_target_x_m": obstacle_scenario.get(
                "recommended_avoidance_target_x_m"
            ),
            "recommended_avoidance_target_y_m": obstacle_scenario.get(
                "recommended_avoidance_target_y_m"
            ),
        },
    )


def _deterministic_return_home_after_failure_recovery_proposal(
    *,
    proposal_id: str,
    runtime_failure_context: Mapping[str, Any],
    runtime_motion_context: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
) -> MissionAutonomyRecoveryProposal:
    return build_mission_autonomy_recovery_proposal(
        mission_ref=proposal_id,
        proposal_source="deterministic_fallback",
        selected_action="return_home",
        reason=(
            "A runtime Nav2 segment failed without completion; propose a bounded "
            "return_home recovery instead of claiming delivery completion."
        ),
        input_observations={
            "runtime_failure_observed": runtime_failure_context.get(
                "runtime_failure_observed"
            ),
            "failed_segment_index": runtime_failure_context.get(
                "failed_segment_index"
            ),
            "failed_segment_label": runtime_failure_context.get(
                "failed_segment_label"
            ),
            "runtime_failure_source": runtime_failure_context.get(
                "runtime_failure_source"
            ),
            "failed_segment_completion_claimed": runtime_failure_context.get(
                "failed_segment_completion_claimed"
            ),
            "failed_segment_blocking_reason_count": runtime_failure_context.get(
                "failed_segment_blocking_reason_count"
            ),
            "recommended_recovery_action": runtime_failure_context.get(
                "recommended_recovery_action"
            ),
            "robot_motion_observed": runtime_motion_context.get(
                "robot_motion_observed"
            ),
            "odom_delta_m": runtime_motion_context.get("odom_delta_m"),
            "motion_observation_source": runtime_motion_context.get(
                "motion_observation_source"
            ),
            "stalled_after_dispatch": runtime_motion_context.get(
                "stalled_after_dispatch"
            ),
            "route_progress_delta_m": runtime_motion_context.get(
                "route_progress_delta_m"
            ),
            "distance_to_home_m": home_distance_envelope.get("distance_to_home_m"),
            "distance_to_home_source": home_distance_envelope.get(
                "distance_to_home_source"
            ),
        },
    )


def _build_recovery_proposals(
    *,
    proposal_id: str,
    operator_instruction: str,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
    autonomy_envelope: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any],
    indoor_delivery_route: Mapping[str, Any],
    runtime_failure_context: Mapping[str, Any] | None = None,
    runtime_motion_context: Mapping[str, Any] | None = None,
    runtime_observation_phase: bool = False,
) -> tuple[tuple[MissionAutonomyRecoveryProposal, ...], dict[str, Any]]:
    battery_recovery_required = battery_envelope.get("dispatch_allowed") is False
    obstacle_recovery_required = (
        runtime_observation_phase
        and obstacle_scenario.get("runtime_obstacle_recovery_required") is True
    )
    failure_context = (
        runtime_failure_context
        if isinstance(runtime_failure_context, Mapping)
        else {}
    )
    failure_recovery_required = (
        failure_context.get("runtime_failure_observed") is True
    )
    motion_context = (
        runtime_motion_context
        if isinstance(runtime_motion_context, Mapping)
        else {}
    )
    if (
        not battery_recovery_required
        and not obstacle_recovery_required
        and not failure_recovery_required
    ):
        return (), _planner_not_required_result()
    planner_result = run_turtlebot3_recovery_planner(
        mission_ref=proposal_id,
        operator_instruction=operator_instruction,
        battery_envelope=battery_envelope,
        home_distance_envelope=home_distance_envelope,
        autonomy_envelope=autonomy_envelope,
        obstacle_scenario=obstacle_scenario,
        indoor_delivery_route=indoor_delivery_route,
        runtime_failure_context=failure_context,
        runtime_motion_context=motion_context,
    )
    planner_proposal = planner_result.get("proposal")
    if (
        planner_result.get("planner_status") == "proposal_guardrail_passed"
        and isinstance(planner_proposal, Mapping)
        and planner_proposal
    ):
        return (
            MissionAutonomyRecoveryProposal.model_validate(dict(planner_proposal)),
        ), dict(planner_result)
    return (
        _deterministic_return_home_recovery_proposal(
            proposal_id=proposal_id,
            battery_envelope=battery_envelope,
            home_distance_envelope=home_distance_envelope,
        )
        if battery_recovery_required
        else _deterministic_return_home_after_failure_recovery_proposal(
            proposal_id=proposal_id,
            runtime_failure_context=failure_context,
            runtime_motion_context=motion_context,
            home_distance_envelope=home_distance_envelope,
        )
        if failure_recovery_required
        else _deterministic_avoid_obstacle_recovery_proposal(
            proposal_id=proposal_id,
            obstacle_scenario=obstacle_scenario,
        ),
    ), dict(planner_result)


def _classify_recovery_proposals(
    *,
    autonomy_envelope: Mapping[str, Any],
    recovery_proposals: tuple[MissionAutonomyRecoveryProposal, ...]
    | tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        classify_mission_autonomy_recovery_proposal(
            envelope=autonomy_envelope,
            proposal=proposal,
        ).model_dump(mode="json")
        for proposal in recovery_proposals
    )


def _fresh_recovery_operator_approval_from_env(
    *,
    selected_action: str | None,
    recovery_proposals: tuple[Mapping[str, Any], ...],
    recovery_proposal_classifications: tuple[Mapping[str, Any], ...],
    approved_at: datetime,
) -> dict[str, Any]:
    approval_ref = os.environ.get(TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF_ENV, "")
    approval_ref = approval_ref.strip()
    if not approval_ref or not recovery_proposals or not recovery_proposal_classifications:
        return {}
    proposal = recovery_proposals[0]
    classification = recovery_proposal_classifications[0]
    if str(proposal.get("selected_action") or "") != str(selected_action or ""):
        return {}
    if classification.get("requires_new_human_approval") is not True:
        return {}
    if classification.get("execution_class") != "requires_human_approval":
        return {}
    actor = os.environ.get(TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_ACTOR_ENV, "")
    actor = actor.strip() or "missionos_chat_operator"
    return {
        "schema_version": "missionos_turtlebot3_recovery_operator_approval.v1",
        "approval_status": "approved",
        "operator_approval_ref": approval_ref,
        "approval_actor": actor,
        "approved_at": approved_at.isoformat(),
        "approved_action": str(selected_action or ""),
        "proposal_ref": proposal.get("proposal_id") or proposal.get("mission_ref"),
        "classification_ref": classification.get("classification_id"),
        "execution_class_before_approval": classification.get("execution_class"),
        "requires_new_human_approval_satisfied": True,
        "approval_source": "operator_e2e_harness",
        "dispatch_authority_created_by_operator_approval": True,
        "proposal_dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
        "progress_counted": False,
    }


def _build_obstacle_scenario(text: str) -> dict[str, Any]:
    requested = _instruction_requests_obstacle_challenge(text)
    runtime_recovery_requested = _instruction_requests_mid_mission_obstacle_recovery(text)
    return {
        "schema_version": "missionos_turtlebot3_obstacle_scenario.v1",
        "obstacle_challenge_requested": requested,
        "runtime_obstacle_recovery_requested": runtime_recovery_requested,
        "runtime_obstacle_recovery_trigger_after_segment_index": 1
        if runtime_recovery_requested
        else None,
        "runtime_obstacle_recovery_required": False,
        "runtime_obstacle_observed": False,
        "costmap_obstacle_observed": False,
        "runtime_obstacle_source": None,
        "recommended_recovery_action": "avoid_obstacle"
        if runtime_recovery_requested
        else None,
        "recommended_avoidance_target_x_m": (
            _profile_dynamic_obstacle_avoidance_goal().x_m
            if runtime_recovery_requested
            else None
        ),
        "recommended_avoidance_target_y_m": (
            _profile_dynamic_obstacle_avoidance_goal().y_m
            if runtime_recovery_requested
            else None
        ),
        "obstacle_inserted_by_missionos": False,
        "obstacle_scene_ref": None,
        "expected_runtime_observation": (
            "obstacle_avoidance_observed" if requested else None
        ),
        "claim_boundary": (
            "Nav2 leg completion is insufficient for obstacle-avoidance completion "
            "unless the bridge reports obstacle_avoidance_observed=true and the "
            "display-aligned odom trajectory clears the obstacle marker bbox."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _build_indoor_delivery_route(
    mission_kind: TurtleBot3HomeMissionKind,
    destination_room: str | None = None,
) -> dict[str, Any]:
    requested = mission_kind == "indoor_delivery_route_leg"
    destination_record = (
        _TURTLEBOT3_HOUSE_ROOM_DESTINATIONS.get(destination_room or "")
        if _profile_is_house()
        else None
    )
    return {
        "destination_room_id": (
            (destination_room or _TURTLEBOT3_HOUSE_DEFAULT_DESTINATION)
            if requested and _profile_is_house()
            else None
        ),
        "destination_room_label": (
            destination_record["label"]
            if requested and destination_record
            else "Living room"
            if requested and _profile_is_house()
            else None
        ),
        "schema_version": "missionos_turtlebot3_indoor_delivery_route.v1",
        "route_requested": requested,
        "route_layout": "simulated_home_loop_with_closed_door_person_and_pet_detours"
        if requested
        else None,
        "pickup_label": "simulated_home_pickup_zone" if requested else None,
        "dropoff_label": "simulated_home_dropoff_zone" if requested else None,
        "planned_room_sequence": (
            _profile_room_sequence(destination_room) if requested else []
        ),
        "simulated_blockers": [
            {
                "name": str(marker["name"]),
                "kind": str(marker["kind"]),
                "label": str(marker["label"]),
            }
            for marker in _profile_layout_obstacles()
        ]
        if requested
        else [],
        "dropoff_arrival_claim_requires": (
            "adapter sim_action completion with odom motion"
            if requested
            else None
        ),
        "payload_pickup_claimed": False,
        "payload_delivery_completion_claimed": False,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _copy_turtlebot3_static_floor_plan() -> dict[str, Any]:
    return {
        **_TURTLEBOT3_HOME_FLOOR_PLAN,
        "bounds": dict(_TURTLEBOT3_HOME_FLOOR_PLAN["bounds"]),
        "geometry_sources": dict(_TURTLEBOT3_HOME_FLOOR_PLAN["geometry_sources"]),
        "wall_polygon": [
            dict(point) for point in _TURTLEBOT3_HOME_FLOOR_PLAN["wall_polygon"]
        ],
        "pillars": [dict(item) for item in _TURTLEBOT3_HOME_FLOOR_PLAN["pillars"]],
        "rooms": [dict(room) for room in _TURTLEBOT3_HOME_FLOOR_PLAN["rooms"]],
        "furniture": [
            dict(item) for item in _TURTLEBOT3_HOME_FLOOR_PLAN["furniture"]
        ],
    }


def _turtlebot3_home_floor_plan(
    robot_profile: TurtleBotNav2RobotProfile = "turtlebot3",
) -> dict[str, Any]:
    if robot_profile == "turtlebot4":
        plan = _copy_turtlebot3_static_floor_plan()
        plan.update(
            {
                "floor_plan_id": "missionos_turtlebot4_indoor_fixture.v1",
                "source": "missionos_static_indoor_nav2_fixture",
                "geometry_sources": {
                    "wall_polygon": (
                        "MissionOS static indoor local-XY display fixture; "
                        "not extracted from TurtleBot4 simulator geometry"
                    ),
                    "pillars": "missionos_display_only_nav2_obstacle_markers",
                    "rooms": "missionos_narrative_overlay_display_only",
                },
                "claim_boundary": (
                    "This is a display-only indoor route fixture for "
                    "TurtleBot4/Nav2 MissionOS artifacts. It does not claim "
                    "the TurtleBot4 simulator world was parsed or physically run."
                ),
            }
        )
        return plan
    if _profile_is_house():
        return _house_floor_plan()
    return _copy_turtlebot3_static_floor_plan()


def _planned_segments_for_mission(
    mission_kind: TurtleBot3HomeMissionKind,
    obstacle_scenario: Mapping[str, Any] | None = None,
    destination_room: str | None = None,
) -> tuple[Nav2GoalPose, ...]:
    if mission_kind == "indoor_delivery_route_leg":
        obstacle = obstacle_scenario if isinstance(obstacle_scenario, Mapping) else {}
        if obstacle.get("runtime_obstacle_recovery_requested") is True:
            return (
                _profile_dynamic_obstacle_approach_segment(),
                *_profile_route_segments(destination_room)[1:],
            )
        return _profile_route_segments(destination_room)
    return (_profile_goal(),)


def _planned_route_distance_m(goals: tuple[Nav2GoalPose, ...]) -> float:
    distance = 0.0
    previous = _profile_home_pose()
    for goal in goals:
        distance += math.hypot(goal.x_m - previous.x_m, goal.y_m - previous.y_m)
        previous = goal
    return distance


def _runtime_recovery_battery_envelope(
    battery_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    battery_pct = battery_envelope.get("runtime_recovery_battery_pct")
    if not isinstance(battery_pct, (int, float)) or isinstance(battery_pct, bool):
        battery_pct = battery_envelope.get("battery_start_pct")
    minimum_required_pct = battery_envelope.get("minimum_required_pct")
    if not isinstance(minimum_required_pct, (int, float)) or isinstance(
        minimum_required_pct,
        bool,
    ):
        minimum_required_pct = 28.0
    dispatch_allowed = float(battery_pct or 0.0) >= float(minimum_required_pct)
    return {
        **dict(battery_envelope),
        "schema_version": "missionos_turtlebot3_runtime_recovery_battery_envelope.v1",
        "battery_state_source": "simulated_runtime_battery_after_segment",
        "battery_start_pct": float(battery_pct or 0.0),
        "dispatch_allowed": dispatch_allowed,
        "blocking_reasons": []
        if dispatch_allowed
        else ["battery_below_minimum_required"],
        "runtime_recovery_observed": True,
    }


def _runtime_recovery_obstacle_scenario(
    obstacle_scenario: Mapping[str, Any],
    *,
    segment_result: Mapping[str, Any],
) -> dict[str, Any]:
    obstacle_observed = (
        segment_result.get("obstacle_detected") is True
        or segment_result.get("costmap_obstacle_observed") is True
        or obstacle_scenario.get("runtime_obstacle_recovery_requested") is True
    )
    return {
        **dict(obstacle_scenario),
        "schema_version": "missionos_turtlebot3_runtime_obstacle_recovery_scenario.v1",
        "runtime_obstacle_recovery_required": obstacle_observed,
        "runtime_obstacle_observed": obstacle_observed,
        "costmap_obstacle_observed": (
            segment_result.get("costmap_obstacle_observed") is True
            or obstacle_observed
        ),
        "runtime_obstacle_source": "ros2_nav2_bridge_costmap"
        if segment_result.get("costmap_obstacle_observed") is True
        else "operator_requested_mid_mission_obstacle_fault",
        "runtime_obstacle_x_m": _profile_delivery_obstacle_xy()[0],
        "runtime_obstacle_y_m": _profile_delivery_obstacle_xy()[1],
        "recommended_recovery_action": "avoid_obstacle",
        "recommended_avoidance_target_x_m": (
            _profile_dynamic_obstacle_avoidance_goal().x_m
        ),
        "recommended_avoidance_target_y_m": (
            _profile_dynamic_obstacle_avoidance_goal().y_m
        ),
        "runtime_recovery_observed": True,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _build_ai_judgment_points(
    *,
    battery_envelope: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any],
) -> tuple[TurtleBot3MissionJudgmentPoint, ...]:
    points = [
        TurtleBot3MissionJudgmentPoint(
            point_id="battery_envelope_before_dispatch",
            judgment_kind="battery_envelope",
            decision="allow"
            if battery_envelope.get("dispatch_allowed") is True
            else "block",
            dispatch_allowed=battery_envelope.get("dispatch_allowed") is True,
            input_observations=dict(battery_envelope),
            blocking_reasons=tuple(
                str(reason)
                for reason in battery_envelope.get("blocking_reasons") or []
            ),
            claim_boundary=(
                "If battery_start_pct is below minimum_required_pct, MissionOS "
                "must not dispatch the TurtleBot3 Nav2 leg."
            ),
        )
    ]
    if obstacle_scenario.get("obstacle_challenge_requested") is True:
        points.append(
            TurtleBot3MissionJudgmentPoint(
                point_id="obstacle_avoidance_runtime_observation",
                judgment_kind="obstacle_avoidance",
                decision="observe_required",
                dispatch_allowed=True,
                input_observations=dict(obstacle_scenario),
                required_runtime_observation="obstacle_avoidance_observed",
                claim_boundary=(
                    "MissionOS may claim the Nav2 leg only from adapter evidence, "
                    "and may claim obstacle avoidance only when the ROS2 bridge "
                    "reports obstacle_avoidance_observed=true and the observed "
                    "trajectory clears the obstacle marker bbox."
                ),
            )
        )
    return tuple(points)


def build_turtlebot3_home_mission_plan(
    *,
    operator_instruction: str,
    robot_profile: Any = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a planning-only TurtleBot3 home mission context."""

    del now
    mission_kind = infer_turtlebot3_home_mission_kind(operator_instruction)
    selected_profile = (
        normalize_turtlebot_nav2_robot_profile(robot_profile)
        if str(robot_profile or "").strip()
        else infer_turtlebot_home_robot_profile(operator_instruction)
    )
    proposal_id = _stable_proposal_id(
        operator_instruction,
        mission_kind,
        selected_profile,
    )
    profile_spec = _robot_profile_spec(selected_profile)
    obstacle_scenario = _build_obstacle_scenario(operator_instruction)
    destination_room = (
        house_destination_room_from_instruction(operator_instruction)
        if _profile_is_house()
        else None
    )
    planned_segments = _planned_segments_for_mission(
        mission_kind,
        obstacle_scenario=obstacle_scenario,
        destination_room=destination_room,
    )
    planned_route_distance = _planned_route_distance_m(planned_segments)
    battery_envelope = _build_battery_envelope(
        operator_instruction,
        planned_route_distance_m=planned_route_distance,
    )
    final_goal = planned_segments[-1]
    home_distance_envelope = _build_home_distance_envelope(
        operator_instruction,
        final_goal,
    )
    indoor_delivery_route = _build_indoor_delivery_route(
        mission_kind,
        destination_room=destination_room,
    )
    autonomy_envelope = _build_autonomy_envelope(proposal_id=proposal_id)
    recovery_proposals, recovery_planner_result = _build_recovery_proposals(
        proposal_id=proposal_id,
        operator_instruction=operator_instruction,
        battery_envelope=battery_envelope,
        home_distance_envelope=home_distance_envelope,
        autonomy_envelope=autonomy_envelope,
        obstacle_scenario=obstacle_scenario,
        indoor_delivery_route=indoor_delivery_route,
    )
    recovery_proposal_classifications = _classify_recovery_proposals(
        autonomy_envelope=autonomy_envelope,
        recovery_proposals=recovery_proposals,
    )
    ai_judgment_points = _build_ai_judgment_points(
        battery_envelope=battery_envelope,
        obstacle_scenario=obstacle_scenario,
    )
    plan = TurtleBot3HomeMissionPlan(
        proposal_id=proposal_id,
        mission_kind=mission_kind,
        mission_objective=_mission_objective(
            mission_kind,
            robot_label=profile_spec["robot_label"],
        ),
        operator_instruction=operator_instruction,
        robot_profile=selected_profile,
        robot_label=profile_spec["robot_label"],
        robot_model=profile_spec["robot_model"],
        execution_target=profile_spec["execution_target"],
        runtime_substrate=profile_spec["runtime_substrate"],
        runtime_profile=profile_spec["runtime_profile"],
        nav2_goal_pose=final_goal,
        planned_segments=planned_segments,
        planned_route_distance_m=planned_route_distance,
        ai_judgment_points=ai_judgment_points,
        obstacle_scenario=dict(obstacle_scenario),
        battery_envelope=dict(battery_envelope),
        home_distance_envelope=dict(home_distance_envelope),
        autonomy_envelope=dict(autonomy_envelope),
        recovery_planner_result=dict(recovery_planner_result),
        recovery_proposals=tuple(
            proposal.model_dump(mode="json") for proposal in recovery_proposals
        ),
        recovery_proposal_classifications=recovery_proposal_classifications,
        indoor_delivery_route=dict(indoor_delivery_route),
        supported_claims=(
            "bounded_nav2_goal_pose",
            "nav2_action_server_ack",
            "odom_motion_observed",
            "sim_action_completion",
            "indoor_delivery_route_arrival",
            "battery_dispatch_block",
            "source_bound_home_distance_projection",
            "recovery_proposal_recorded",
            "autonomy_envelope_execution_classification",
            "obstacle_avoidance_observed",
            "runtime_avoid_obstacle_recovery_dispatch",
            "route_resume_after_obstacle_recovery",
        ),
        blocked_claims=_blocked_claims(mission_kind),
        safety_boundaries=(
            "operator_approval_required",
            "autonomy_envelope_classifies_execution_not_proposal",
            "recovery_proposal_does_not_self_approve_or_dispatch",
            "recovery_observations_must_be_source_backed",
            "battery_envelope_checked_before_dispatch",
            "obstacle_avoidance_claim_requires_bridge_observation",
            "runtime_avoid_obstacle_requires_source_backed_obstacle_observation",
            "runtime_avoid_obstacle_recovery_is_bounded_nav2_goal_not_raw_velocity",
            "indoor_delivery_route_claim_is_dropoff_arrival_only",
            "raw_cmd_vel_not_generated_by_missionos",
            "nav2_succeeded_without_odom_motion_is_not_completion",
            "sim_action_is_not_physical_execution",
            "cleaning_and_payload_claims_require_separate_actuators_and_verifiers",
        ),
    )
    plan_payload = plan.model_dump(mode="json")
    validation_result = {
        "schema_version": "missionos_turtlebot3_home_mission_validation.v1",
        "validation_status": "accepted",
        "accepted_action": "nav2_goal_pose",
        "accepted_scope": "bounded_sim_nav2_route_segments",
        "robot_profile": plan.robot_profile,
        "robot_label": plan.robot_label,
        "execution_target": plan.execution_target,
        "runtime_substrate": plan.runtime_substrate,
        "runtime_profile": plan.runtime_profile,
        "ai_judgment_points": [
            point.model_dump(mode="json") for point in ai_judgment_points
        ],
        "rejected_claims": list(plan.blocked_claims),
        "dispatch_authority_created": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
        "planned_route_distance_m": planned_route_distance,
        "autonomy_envelope": dict(autonomy_envelope),
        "home_distance_envelope": dict(home_distance_envelope),
        "recovery_planner_result": dict(recovery_planner_result),
        "recovery_proposals": [
            proposal.model_dump(mode="json") for proposal in recovery_proposals
        ],
        "recovery_proposal_classifications": list(
            recovery_proposal_classifications
        ),
    }
    return {
        "scenario_proposal": plan_payload,
        "validation_result": validation_result,
        "turtlebot3_home_mission_plan": plan_payload,
        "summary": {
            "status": "proposal",
            "mission_domain": "home_robot",
            "mission_objective": plan.mission_objective,
            "home_robot_mission_kind": mission_kind,
            "robot_profile": plan.robot_profile,
            "robot_label": plan.robot_label,
            "robot_model": plan.robot_model,
            "execution_target": plan.execution_target,
            "runtime_substrate": plan.runtime_substrate,
            "runtime_profile": plan.runtime_profile,
            "execution_mode": plan.execution_mode,
            "nav2_goal_pose": plan.nav2_goal_pose.model_dump(mode="json"),
            "planned_segments": [
                segment.model_dump(mode="json") for segment in planned_segments
            ],
            "planned_segment_count": len(planned_segments),
            "planned_route_distance_m": planned_route_distance,
            "ai_judgment_points": [
                point.model_dump(mode="json") for point in ai_judgment_points
            ],
            "battery_envelope": dict(battery_envelope),
            "home_distance_envelope": dict(home_distance_envelope),
            "autonomy_envelope": dict(autonomy_envelope),
            "recovery_planner_result": dict(recovery_planner_result),
            "recovery_planner_status": recovery_planner_result.get(
                "planner_status"
            ),
            "recovery_proposals": [
                proposal.model_dump(mode="json") for proposal in recovery_proposals
            ],
            "recovery_proposal_classifications": list(
                recovery_proposal_classifications
            ),
            "llm_recovery_proposals_allowed": True,
            "proposal_first_classification": True,
            "recovery_execution_permitted_by_envelope": any(
                classification.get("execution_permitted_by_envelope") is True
                for classification in recovery_proposal_classifications
            ),
            "obstacle_scenario": dict(obstacle_scenario),
            "indoor_delivery_route": dict(indoor_delivery_route),
            "supported_claims": list(plan.supported_claims),
            "blocked_claims": list(plan.blocked_claims),
            "requires_operator_approval": True,
            "dispatch_request_sent": False,
            "completion_claimed": False,
            "completion_scope": "none",
            "physical_execution_invoked": False,
            "indoor_delivery_route_completion_claimed": False,
            "mission_delivery_completion_claimed": False,
            "progress_counted": False,
        },
    }


def approve_turtlebot3_home_mission_plan(
    *,
    proposal: Mapping[str, Any],
    validation: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record human approval for bounded TurtleBot3 route segments."""

    approved_at = now or datetime.now(timezone.utc)
    robot_profile = _robot_profile_from_proposal(proposal)
    profile_spec = _robot_profile_spec(robot_profile)
    if str(validation.get("validation_status") or "") != "accepted":
        raise ValueError("TurtleBot3 mission approval requires accepted validation")
    approval_ref = f"approval_{proposal.get('proposal_id') or 'turtlebot3_home_mission'}"
    proposed_envelope = proposal.get("autonomy_envelope")
    if isinstance(proposed_envelope, Mapping):
        autonomy_envelope = approve_mission_autonomy_envelope(
            proposed_envelope,
            operator_approval_ref=approval_ref,
        ).model_dump(mode="json")
    else:
        autonomy_envelope = _build_autonomy_envelope(
            proposal_id=str(proposal.get("proposal_id") or "turtlebot3_home_mission"),
            operator_approved=True,
            operator_approval_ref=approval_ref,
        )
    recovery_proposals = tuple(
        dict(item)
        for item in proposal.get("recovery_proposals") or ()
        if isinstance(item, Mapping)
    )
    recovery_planner_result = proposal.get("recovery_planner_result")
    recovery_planner_result = (
        dict(recovery_planner_result)
        if isinstance(recovery_planner_result, Mapping)
        else {}
    )
    home_distance_envelope = proposal.get("home_distance_envelope")
    home_distance_envelope = (
        dict(home_distance_envelope)
        if isinstance(home_distance_envelope, Mapping)
        else {}
    )
    recovery_proposal_classifications = _classify_recovery_proposals(
        autonomy_envelope=autonomy_envelope,
        recovery_proposals=recovery_proposals,
    )
    approval = {
        "schema_version": TURTLEBOT3_HOME_MISSION_APPROVAL_SCHEMA,
        "approval_status": "approved",
        "operator_approved": True,
        "operator_approval_ref": approval_ref,
        "approval_actor": "missionos_chat_operator",
        "approved_at": approved_at.isoformat(),
        "approved_scope": "bounded_sim_nav2_route_segments",
        "approved_action": "nav2_goal_pose",
        "autonomy_envelope": dict(autonomy_envelope),
        "recovery_proposal_classifications": list(
            recovery_proposal_classifications
        ),
        "llm_recovery_proposals_allowed": True,
        "proposal_first_classification": True,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
    }
    return {
        "turtlebot3_home_mission_approval": approval,
        "turtlebot3_bounded_nav2_request": {
            "schema_version": "missionos_turtlebot3_bounded_nav2_request.v1",
            "request_status": "approved_for_operator_requested_dispatch",
            "robot_profile": robot_profile,
            "robot_label": profile_spec["robot_label"],
            "robot_model": str(
                proposal.get("robot_model") or profile_spec["robot_model"]
            ),
            "execution_target": profile_spec["execution_target"],
            "runtime_substrate": profile_spec["runtime_substrate"],
            "runtime_profile": profile_spec["runtime_profile"],
            "execution_mode": "sim",
            "goal_pose": dict(proposal.get("nav2_goal_pose") or {}),
            "planned_segments": [
                dict(segment)
                for segment in proposal.get("planned_segments") or ()
                if isinstance(segment, Mapping)
            ],
            "requires_explicit_operator_execution_request": True,
            "dispatch_invoked": False,
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        },
        "summary": {
            "status": "approved",
            "approval_status": "approved",
            "operator_approved": True,
            "operator_approval_ref": approval_ref,
            "approved_scope": "bounded_sim_nav2_route_segments",
            "robot_profile": robot_profile,
            "robot_label": profile_spec["robot_label"],
            "robot_model": str(
                proposal.get("robot_model") or profile_spec["robot_model"]
            ),
            "execution_target": profile_spec["execution_target"],
            "runtime_substrate": profile_spec["runtime_substrate"],
            "runtime_profile": profile_spec["runtime_profile"],
            "autonomy_envelope": dict(autonomy_envelope),
            "recovery_proposal_classifications": list(
                recovery_proposal_classifications
            ),
            "llm_recovery_proposals_allowed": True,
            "proposal_first_classification": True,
            "recovery_execution_permitted_by_envelope": any(
                classification.get("execution_permitted_by_envelope") is True
                for classification in recovery_proposal_classifications
            ),
            "dispatch_request_sent": False,
            "completion_claimed": False,
            "completion_scope": "none",
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
            "progress_counted": False,
        },
    }


def _goal_from_payload(payload: Mapping[str, Any]) -> Nav2GoalPose:
    return Nav2GoalPose.model_validate(dict(payload))


def _bridge_readiness_blocking_reasons(
    robot_profile: Any = "turtlebot3",
) -> tuple[str, ...]:
    profile_spec = _robot_profile_spec(robot_profile)
    reasons: list[str] = []
    if not os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip():
        reasons.append(profile_spec["bridge_not_configured_reason"])
    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        reasons.append(profile_spec["runtime_not_enabled_reason"])
    return tuple(reasons)


def _runtime_configuration_status(blocking_reasons: list[str]) -> str:
    not_configured = {
        str(spec["bridge_not_configured_reason"])
        for spec in _TURTLEBOT_NAV2_PROFILE_SPECS.values()
    }
    not_configured.update(
        str(spec["runtime_not_enabled_reason"])
        for spec in _TURTLEBOT_NAV2_PROFILE_SPECS.values()
    )
    not_configured.add("nvblox_perception_evidence_not_configured")
    return (
        "not_configured"
        if any(reason in not_configured for reason in blocking_reasons)
        else "configured"
    )


def _turtlebot3_raw_logs_ref_from_env(
    robot_profile: TurtleBotNav2RobotProfile = "turtlebot3",
) -> str | None:
    if robot_profile != "turtlebot3":
        return None
    try:
        return turtlebot3_log_bundle_ref_from_env()
    except TurtleBot3LogCollectorError:
        return None


def _turtlebot3_log_bundle_artifacts(
    robot_profile: TurtleBotNav2RobotProfile = "turtlebot3",
) -> dict[str, Any]:
    if robot_profile != "turtlebot3":
        return {}
    try:
        bundle = collect_turtlebot3_log_bundle_from_env()
    except TurtleBot3LogCollectorError as exc:
        raw_logs_ref = _turtlebot3_raw_logs_ref_from_env(robot_profile)
        return {
            "log_bundle_status": "blocked",
            "raw_logs_ref": raw_logs_ref,
            "blocking_reasons": ["turtlebot3_log_bundle_env_invalid"],
            "collector_error": str(exc),
            "raw_logs_included": False,
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        }
    if bundle is None:
        return {}
    nav2_log_diagnostics = build_turtlebot3_nav2_log_diagnostics(bundle)
    return {
        "log_bundle_status": bundle.bundle_status,
        "raw_logs_ref": bundle.raw_logs_ref,
        "turtlebot3_log_bundle": bundle.model_dump(mode="json"),
        "nav2_log_diagnostics": nav2_log_diagnostics.model_dump(mode="json"),
        "nav2_log_diagnostics_status": nav2_log_diagnostics.diagnostic_status,
        "nav2_log_observed_patterns": list(nav2_log_diagnostics.observed_patterns),
        "nav2_log_failure_hypotheses": list(
            nav2_log_diagnostics.failure_hypotheses
        ),
        "observed_source_count": bundle.observed_source_count,
        "source_count": bundle.source_count,
        "missing_required_sources": list(bundle.missing_required_sources),
        "blocking_reasons": list(bundle.blocked_reasons),
        "raw_logs_included": False,
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
    }


def _pre_dispatch_judgment_blocking_reasons(
    proposal: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons: list[str] = []
    for point in proposal.get("ai_judgment_points") or []:
        if not isinstance(point, Mapping):
            continue
        if point.get("dispatch_allowed") is False:
            reasons.extend(str(reason) for reason in point.get("blocking_reasons") or [])
    battery = proposal.get("battery_envelope")
    if isinstance(battery, Mapping) and battery.get("dispatch_allowed") is False:
        reasons.extend(str(reason) for reason in battery.get("blocking_reasons") or [])
    return tuple(dict.fromkeys(reason for reason in reasons if reason))


def _robot_motion_from_responses(responses: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    for response in responses:
        state = response.get("state_result")
        if isinstance(state, Mapping):
            return {
                "robot_motion_observed": state.get("robot_motion_observed") is True,
                "odom_delta_m": state.get("odom_delta_m"),
                "odom_topic": state.get("odom_topic"),
                "robot_motion_observation_source": "ros2_nav2_bridge_receipt",
            }
    return {
        "robot_motion_observed": False,
        "odom_delta_m": None,
        "odom_topic": None,
        "robot_motion_observation_source": "not_available",
    }


def _turtlebot3_sidecar_motion_artifacts(
    *,
    bridge_motion: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], bool]:
    jsonl_path = os.environ.get(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, "").strip()
    if not jsonl_path:
        return {}, dict(bridge_motion), [], False
    try:
        window = build_turtlebot3_telemetry_window_from_jsonl(jsonl_path)
        correlation = build_turtlebot3_state_correlation(
            telemetry_window=window,
            bridge_motion=bridge_motion,
        )
    except TurtleBot3TelemetrySidecarError as exc:
        return (
            {
                "telemetry_sidecar_status": "blocked",
                "telemetry_sidecar_jsonl_path": jsonl_path,
                "telemetry_sidecar_error": str(exc),
                "physical_execution_invoked": False,
                "mission_delivery_completion_claimed": False,
            },
            {
                "robot_motion_observed": False,
                "odom_delta_m": None,
                "odom_topic": None,
                "robot_motion_observation_source": "telemetry_sidecar_error",
            },
            ["telemetry_sidecar_unreadable"],
            True,
        )

    window_payload = window.model_dump(mode="json")
    correlation_payload = correlation.model_dump(mode="json")
    motion = {
        "robot_motion_observed": window.odom_motion_observed,
        "odom_delta_m": window.odom_delta_m,
        "odom_topic": window.odom_topic,
        "robot_motion_observation_source": "ros2_telemetry_sidecar_jsonl",
        "telemetry_window_ref": correlation.telemetry_window_ref,
        "telemetry_raw_logs_ref": window.raw_logs_ref,
    }
    blocking_reasons = (
        list(correlation.blocked_reasons)
        if correlation.correlation_status == "blocked"
        else []
    )
    return (
        {
            "telemetry_sidecar_status": correlation.correlation_status,
            "turtlebot3_telemetry_window": window_payload,
            "turtlebot3_state_correlation": correlation_payload,
            "telemetry_window_ref": correlation.telemetry_window_ref,
            "raw_logs_ref": window.raw_logs_ref,
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        },
        motion,
        blocking_reasons,
        True,
    )


def _obstacle_observation_from_responses(
    responses: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    for response in responses:
        state = response.get("state_result")
        progress = response.get("progress_result")
        state = state if isinstance(state, Mapping) else {}
        progress = progress if isinstance(progress, Mapping) else {}
        trajectory = response.get("trajectory_result")
        if not isinstance(trajectory, Mapping):
            trajectory = state.get("trajectory_result")
        if not isinstance(trajectory, Mapping):
            trajectory = progress.get("trajectory_result")
        trajectory = trajectory if isinstance(trajectory, Mapping) else {}
        obstacle_detected = (
            response.get("obstacle_detected") is True
            or response.get("costmap_obstacle_observed") is True
            or state.get("obstacle_detected") is True
            or state.get("costmap_obstacle_observed") is True
            or progress.get("obstacle_detected") is True
            or progress.get("costmap_obstacle_observed") is True
        )
        obstacle_avoidance_observed = (
            response.get("obstacle_avoidance_observed") is True
            or state.get("obstacle_avoidance_observed") is True
            or progress.get("obstacle_avoidance_observed") is True
        )
        if obstacle_detected or obstacle_avoidance_observed:
            return {
                "obstacle_detected": obstacle_detected,
                "costmap_obstacle_observed": (
                    response.get("costmap_obstacle_observed") is True
                    or state.get("costmap_obstacle_observed") is True
                    or progress.get("costmap_obstacle_observed") is True
                ),
                "obstacle_avoidance_observed": obstacle_avoidance_observed,
                "trajectory_lateral_deviation_observed": (
                    trajectory.get("trajectory_lateral_deviation_observed") is True
                ),
                "max_lateral_deviation_m": trajectory.get("max_lateral_deviation_m"),
                "avoidance_observation_source": str(
                    response.get("ack_source") or response.get("action") or "bridge"
                ),
            }
    return {
        "obstacle_detected": False,
        "costmap_obstacle_observed": False,
        "obstacle_avoidance_observed": False,
        "trajectory_lateral_deviation_observed": False,
        "max_lateral_deviation_m": None,
        "avoidance_observation_source": None,
    }


def _obstacle_challenge_required(proposal: Mapping[str, Any]) -> bool:
    obstacle = proposal.get("obstacle_scenario")
    return isinstance(obstacle, Mapping) and obstacle.get("obstacle_challenge_requested") is True


def _planned_segment_goals_from_proposal(
    proposal: Mapping[str, Any],
) -> tuple[Nav2GoalPose, ...]:
    goals: list[Nav2GoalPose] = []
    for item in proposal.get("planned_segments") or ():
        if isinstance(item, Mapping):
            goals.append(_goal_from_payload(item))
    if goals:
        return tuple(goals)
    return (_goal_from_payload(proposal.get("nav2_goal_pose") or {}),)


def _indoor_map_point_from_goal(
    goal: Nav2GoalPose,
    *,
    role: str,
    source: str,
    sequence: int,
) -> dict[str, Any]:
    return {
        "x_m": goal.x_m,
        "y_m": goal.y_m,
        "yaw_rad": goal.yaw_rad,
        "frame_id": goal.frame_id,
        "label": goal.label,
        "role": role,
        "source": source,
        "sequence": sequence,
    }


def _planned_indoor_map_points(goals: tuple[Nav2GoalPose, ...]) -> list[dict[str, Any]]:
    planned_points = [
        _indoor_map_point_from_goal(
            _profile_home_pose(),
            role="home",
            source="missionos_turtlebot3_home_pose",
            sequence=0,
        )
    ]
    for index, goal in enumerate(goals, start=1):
        role = "dropoff" if index == len(goals) else "checkpoint"
        planned_points.append(
            _indoor_map_point_from_goal(
                goal,
                role=role,
                source="missionos_planned_nav2_segment",
                sequence=index,
            )
        )
    return planned_points


def _numeric_xy_sample(sample: Mapping[str, Any]) -> dict[str, Any] | None:
    x_m = sample.get("x_m", sample.get("local_x_m", sample.get("x")))
    y_m = sample.get("y_m", sample.get("local_y_m", sample.get("y")))
    if not isinstance(x_m, (int, float)) or isinstance(x_m, bool):
        return None
    if not isinstance(y_m, (int, float)) or isinstance(y_m, bool):
        return None
    return {
        "x_m": float(x_m),
        "y_m": float(y_m),
        "frame_id": str(sample.get("frame_id") or ""),
        "source": str(sample.get("source") or "ros2_nav2_bridge_trajectory"),
        "sample_index": sample.get("sample_index"),
        "elapsed_s": sample.get("elapsed_s") or sample.get("elapsed_seconds"),
    }


def _trajectory_samples_from_response(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    sources = [
        response.get("trajectory_result"),
        response.get("state_result"),
        response.get("progress_result"),
    ]
    for container in sources:
        if not isinstance(container, Mapping):
            continue
        trajectory = container.get("trajectory_result")
        if isinstance(trajectory, Mapping):
            sources.append(trajectory)
        for key in (
            "trajectory_samples",
            "pose_samples",
            "odom_samples",
            "position_samples",
            "path_samples",
        ):
            raw_samples = container.get(key)
            if not isinstance(raw_samples, list):
                continue
            has_map_frame_samples = any(
                isinstance(raw_sample, Mapping)
                and str(raw_sample.get("frame_id") or "").lower() == "map"
                for raw_sample in raw_samples
            )
            for raw_sample in raw_samples:
                if not isinstance(raw_sample, Mapping):
                    continue
                if has_map_frame_samples and str(
                    raw_sample.get("frame_id") or ""
                ).lower() != "map":
                    continue
                sample = _numeric_xy_sample(raw_sample)
                if sample is None:
                    continue
                sample["source"] = str(
                    raw_sample.get("source")
                    or f"ros2_nav2_bridge.{key}"
                )
                samples.append(sample)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[float, float, Any]] = set()
    for index, sample in enumerate(samples):
        key = (
            round(float(sample["x_m"]), 4),
            round(float(sample["y_m"]), 4),
            sample.get("sample_index", index),
        )
        if key in seen:
            continue
        seen.add(key)
        if sample.get("sample_index") is None:
            sample["sample_index"] = index
        deduped.append(sample)
    return deduped


def _observed_points_from_action_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    segment_ref = str(result.get("segment_ref") or "segment")
    for response in result.get("bridge_responses") or ():
        if not isinstance(response, Mapping):
            continue
        for sample in _trajectory_samples_from_response(response):
            points.append(
                {
                    **sample,
                    "segment_ref": segment_ref,
                    "role": "observed_pose",
                }
            )
    return points


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _planned_home_point(planned_points: list[dict[str, Any]]) -> dict[str, Any] | None:
    for point in planned_points:
        if point.get("role") == "home":
            return point
    return planned_points[0] if planned_points else None


def _observed_display_alignment(
    *,
    planned_points: list[dict[str, Any]],
    observed_points: list[dict[str, Any]],
    recovery_points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe the display-only transform from odom-local samples to map layout.

    TurtleBot3 `/odom` samples are local to the robot spawn, while the MissionOS
    indoor layout is expressed in the Nav2 map frame. The raw bridge responses
    remain unmodified; this transform only makes the read-only map overlay line
    up with the planned home pose.
    """

    home = _planned_home_point(planned_points)
    observed_ref = (observed_points or recovery_points or [None])[0]
    if (
        isinstance(observed_ref, Mapping)
        and str(observed_ref.get("frame_id") or "") == "map"
    ):
        return {
            "applied": False,
            "method": "map_frame_samples",
            "dx_m": 0.0,
            "dy_m": 0.0,
            "claim_boundary": (
                "Bridge trajectory samples are already AMCL-corrected map-frame "
                "poses; no display translation is applied."
            ),
        }
    if not isinstance(home, Mapping) or not isinstance(observed_ref, Mapping):
        return {
            "applied": False,
            "method": "not_available",
            "dx_m": 0.0,
            "dy_m": 0.0,
            "claim_boundary": (
                "No display transform was applied because a planned home point "
                "or observed pose sample was unavailable."
            ),
        }
    home_x = _float_or_none(home.get("x_m"))
    home_y = _float_or_none(home.get("y_m"))
    observed_x = _float_or_none(observed_ref.get("x_m"))
    observed_y = _float_or_none(observed_ref.get("y_m"))
    if home_x is None or home_y is None or observed_x is None or observed_y is None:
        return {
            "applied": False,
            "method": "not_numeric",
            "dx_m": 0.0,
            "dy_m": 0.0,
            "claim_boundary": (
                "No display transform was applied because the planned home point "
                "or observed pose sample was not numeric."
            ),
        }
    dx_m = home_x - observed_x
    dy_m = home_y - observed_y
    applied = abs(dx_m) > 1e-6 or abs(dy_m) > 1e-6
    return {
        "applied": applied,
        "method": "first_observed_pose_to_planned_home",
        "dx_m": round(dx_m, 6),
        "dy_m": round(dy_m, 6),
        "raw_trajectory_frame": "bridge_reported_local_xy",
        "display_frame": str(home.get("frame_id") or "map"),
        "home_ref": str(home.get("label") or "planned_home"),
        "claim_boundary": (
            "This transform is for read-only map display only. Raw bridge "
            "trajectory samples remain in bridge_responses and are not changed "
            "for completion, motion, delivery, or physical-execution claims."
        ),
    }


def _apply_observed_display_alignment(
    points: list[dict[str, Any]],
    *,
    alignment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    dx_m = _float_or_none(alignment.get("dx_m")) or 0.0
    dy_m = _float_or_none(alignment.get("dy_m")) or 0.0
    applied = alignment.get("applied") is True
    if not applied:
        return [dict(point) for point in points]

    aligned: list[dict[str, Any]] = []
    for point in points:
        x_m = _float_or_none(point.get("x_m"))
        y_m = _float_or_none(point.get("y_m"))
        if x_m is None or y_m is None:
            aligned.append(dict(point))
            continue
        aligned.append(
            {
                **point,
                "raw_x_m": x_m,
                "raw_y_m": y_m,
                "x_m": x_m + dx_m,
                "y_m": y_m + dy_m,
                "frame_id": str(alignment.get("display_frame") or "map"),
                "display_alignment_applied": True,
                "display_alignment_method": str(alignment.get("method") or ""),
            }
        )
    return aligned


def _turtlebot3_delivery_obstacle_markers(
    *,
    obstacle_required: bool,
    obstacle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not obstacle_required:
        return []
    markers = []
    for scene_marker in _profile_layout_obstacles():
        markers.append(
            {
                "name": scene_marker["name"],
                "kind": scene_marker["kind"],
                "label": scene_marker["label"],
                "x_m": scene_marker["x_m"],
                "y_m": scene_marker["y_m"],
                "size_x_m": scene_marker["size_x_m"],
                "size_y_m": scene_marker["size_y_m"],
                "label_offset_y_px": scene_marker.get("label_offset_y_px"),
                "observed": obstacle.get("costmap_obstacle_observed") is True,
                "avoidance_observed": (
                    obstacle.get("obstacle_avoidance_observed") is True
                ),
                "trajectory_clearance_observed": (
                    obstacle.get("obstacle_trajectory_clearance_observed") is True
                ),
                "trajectory_intersects_obstacle": (
                    obstacle.get("obstacle_trajectory_intersects_obstacle") is True
                ),
                "intersection_point_count": obstacle.get(
                    "obstacle_intersection_point_count"
                ),
                "intersection_segment_count": obstacle.get(
                    "obstacle_intersection_segment_count"
                ),
                "source": "opt_in_turtlebot3_home_loop_obstacle_smoke_scene",
                "claim_boundary": (
                    "This marker is an opt-in TurtleBot3 home-loop smoke scene "
                    "blocker; it is not a discovered home object, person, or pet."
                ),
            }
        )
    return markers


def _rect_from_obstacle_marker(
    obstacle: Mapping[str, Any],
) -> tuple[float, float, float, float] | None:
    x_m = _float_or_none(obstacle.get("x_m"))
    y_m = _float_or_none(obstacle.get("y_m"))
    size_x_m = _float_or_none(obstacle.get("size_x_m"))
    size_y_m = _float_or_none(obstacle.get("size_y_m"))
    if x_m is None or y_m is None or size_x_m is None or size_y_m is None:
        return None
    return (
        x_m - size_x_m / 2.0,
        x_m + size_x_m / 2.0,
        y_m - size_y_m / 2.0,
        y_m + size_y_m / 2.0,
    )


def _point_inside_rect(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    min_x, max_x, min_y, max_y = rect
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def _orientation(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float:
    return (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])


def _on_segment(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    return (
        min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
        and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
    )


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)
    eps = 1e-9
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if abs(o1) <= eps and _on_segment(a1, b1, a2):
        return True
    if abs(o2) <= eps and _on_segment(a1, b2, a2):
        return True
    if abs(o3) <= eps and _on_segment(b1, a1, b2):
        return True
    return abs(o4) <= eps and _on_segment(b1, a2, b2)


def _segment_intersects_rect(
    start: tuple[float, float],
    end: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    if _point_inside_rect(start, rect) or _point_inside_rect(end, rect):
        return True
    min_x, max_x, min_y, max_y = rect
    edges = (
        ((min_x, min_y), (max_x, min_y)),
        ((max_x, min_y), (max_x, max_y)),
        ((max_x, max_y), (min_x, max_y)),
        ((min_x, max_y), (min_x, min_y)),
    )
    return any(_segments_intersect(start, end, edge_start, edge_end) for edge_start, edge_end in edges)


def _point_rect_clearance_m(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> float:
    min_x, max_x, min_y, max_y = rect
    x_m, y_m = point
    if _point_inside_rect(point, rect):
        return -min(x_m - min_x, max_x - x_m, y_m - min_y, max_y - y_m)
    dx_m = max(min_x - x_m, 0.0, x_m - max_x)
    dy_m = max(min_y - y_m, 0.0, y_m - max_y)
    return math.hypot(dx_m, dy_m)


def _obstacle_trajectory_geometry(
    *,
    obstacle_required: bool,
    obstacle: Mapping[str, Any],
    planned_points: list[dict[str, Any]],
    observed_points: list[dict[str, Any]],
    recovery_points: list[dict[str, Any]],
) -> dict[str, Any]:
    markers = _turtlebot3_delivery_obstacle_markers(
        obstacle_required=obstacle_required,
        obstacle=obstacle,
    )
    if not obstacle_required:
        return {
            "obstacle_trajectory_clearance_observed": False,
            "obstacle_trajectory_intersects_obstacle": False,
            "obstacle_intersection_point_count": 0,
            "obstacle_intersection_segment_count": 0,
            "obstacle_min_clearance_m": None,
            "obstacle_trajectory_geometry_source": "not_required",
        }
    display_alignment = _observed_display_alignment(
        planned_points=planned_points,
        observed_points=observed_points,
        recovery_points=recovery_points,
    )
    aligned_points = _apply_observed_display_alignment(
        [*observed_points, *recovery_points],
        alignment=display_alignment,
    )
    xy_points: list[tuple[float, float]] = []
    xy_points_by_segment: dict[str, list[tuple[float, float]]] = {}
    for point in aligned_points:
        x_m = _float_or_none(point.get("x_m"))
        y_m = _float_or_none(point.get("y_m"))
        if x_m is None or y_m is None:
            continue
        xy = (x_m, y_m)
        xy_points.append(xy)
        segment_ref = str(point.get("segment_ref") or "segment")
        xy_points_by_segment.setdefault(segment_ref, []).append(xy)
    rects = [
        rect
        for marker in markers
        if (rect := _rect_from_obstacle_marker(marker)) is not None
    ]
    point_intersections = 0
    segment_intersections = 0
    min_clearance = None
    for rect in rects:
        for point in xy_points:
            clearance = _point_rect_clearance_m(point, rect)
            min_clearance = clearance if min_clearance is None else min(min_clearance, clearance)
            if clearance < 0.0 or _point_inside_rect(point, rect):
                point_intersections += 1
        for segment_points in xy_points_by_segment.values():
            for start, end in zip(segment_points, segment_points[1:], strict=False):
                if _segment_intersects_rect(start, end, rect):
                    segment_intersections += 1
    intersects = point_intersections > 0 or segment_intersections > 0
    return {
        "obstacle_trajectory_clearance_observed": bool(xy_points) and not intersects,
        "obstacle_trajectory_intersects_obstacle": intersects,
        "obstacle_intersection_point_count": point_intersections,
        "obstacle_intersection_segment_count": segment_intersections,
        "obstacle_min_clearance_m": round(min_clearance, 6)
        if min_clearance is not None
        else None,
        "obstacle_trajectory_geometry_source": (
            "display_aligned_bridge_trajectory_vs_obstacle_bbox"
        ),
        "obstacle_trajectory_geometry_claim_boundary": (
            "Obstacle-avoidance completion requires the bridge observation plus "
            "a display-aligned trajectory that does not intersect the obstacle "
            "bbox. This is a simulator evidence check, not physical delivery."
        ),
    }


_TURTLEBOT3_DISPLAY_DECIMATION_EPSILON_M = 0.02
_TURTLEBOT3_DISPLAY_DECIMATION_MIN_POINTS = 200


def _rdp_keep_indices(
    points: list[tuple[float, float]],
    epsilon_m: float,
) -> set[int]:
    """Ramer-Douglas-Peucker on an open polyline; returns retained indices."""

    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        span = math.hypot(bx - ax, by - ay)
        max_distance = -1.0
        max_index = start
        for index in range(start + 1, end):
            px, py = points[index]
            if span < 1e-12:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(
                    (bx - ax) * (ay - py) - (ax - px) * (by - ay)
                ) / span
            if distance > max_distance:
                max_distance = distance
                max_index = index
        if max_distance > epsilon_m:
            keep.add(max_index)
            stack.append((start, max_index))
            stack.append((max_index, end))
    return keep


_TURTLEBOT3_DISPLAY_MAX_SAMPLE_STEP_M = 0.6


def _sanitize_observed_display_points(
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop display samples that cannot be real robot poses.

    Two artefact classes are removed before decimation: odom-frame fallback
    samples that leaked into an otherwise map-frame series (tf not ready yet),
    and isolated single-sample jumps larger than the robot could physically
    travel between consecutive odometry messages. Sustained shifts (AMCL
    corrections) are kept because the following samples confirm them.
    """

    raw_count = len(points)
    map_frame_present = any(
        str(point.get("frame_id") or "") == "map" for point in points
    )
    frame_filtered = [
        point
        for point in points
        if not map_frame_present or str(point.get("frame_id") or "") == "map"
    ]
    kept: list[dict[str, Any]] = []
    spike_dropped = 0
    for index, point in enumerate(frame_filtered):
        if not kept:
            kept.append(point)
            continue
        previous = kept[-1]
        step = math.hypot(
            float(point.get("x_m") or 0.0) - float(previous.get("x_m") or 0.0),
            float(point.get("y_m") or 0.0) - float(previous.get("y_m") or 0.0),
        )
        if step <= _TURTLEBOT3_DISPLAY_MAX_SAMPLE_STEP_M:
            kept.append(point)
            continue
        following = frame_filtered[index + 1] if index + 1 < len(frame_filtered) else None
        confirmed = following is not None and math.hypot(
            float(following.get("x_m") or 0.0) - float(point.get("x_m") or 0.0),
            float(following.get("y_m") or 0.0) - float(point.get("y_m") or 0.0),
        ) <= _TURTLEBOT3_DISPLAY_MAX_SAMPLE_STEP_M
        if confirmed:
            kept.append(point)
        else:
            spike_dropped += 1
    return kept, {
        "raw_point_count": raw_count,
        "odom_fallback_dropped": raw_count - len(frame_filtered),
        "spike_dropped": spike_dropped,
        "max_sample_step_m": _TURTLEBOT3_DISPLAY_MAX_SAMPLE_STEP_M,
    }


def _decimate_observed_display_points(
    points: list[dict[str, Any]],
    *,
    epsilon_m: float = _TURTLEBOT3_DISPLAY_DECIMATION_EPSILON_M,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Thin dense trajectory samples for display without changing evidence.

    Decimation runs per segment_ref so segment boundaries are always kept.
    Raw samples remain in bridge_responses; this only reduces what the
    read-only map overlay draws.
    """

    raw_count = len(points)
    if raw_count <= _TURTLEBOT3_DISPLAY_DECIMATION_MIN_POINTS:
        return points, {
            "applied": False,
            "method": "none_below_threshold",
            "raw_point_count": raw_count,
            "display_point_count": raw_count,
            "epsilon_m": epsilon_m,
        }

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for point in points:
        segment_ref = str(point.get("segment_ref") or "")
        if not groups or groups[-1][0] != segment_ref:
            groups.append((segment_ref, []))
        groups[-1][1].append(point)

    decimated: list[dict[str, Any]] = []
    for _, group in groups:
        xy = [
            (float(point.get("x_m") or 0.0), float(point.get("y_m") or 0.0))
            for point in group
        ]
        if len(xy) < 3:
            decimated.extend(group)
            continue
        keep = _rdp_keep_indices(xy, epsilon_m)
        decimated.extend(
            point for index, point in enumerate(group) if index in keep
        )
    return decimated, {
        "applied": True,
        "method": "ramer_douglas_peucker_per_segment",
        "raw_point_count": raw_count,
        "display_point_count": len(decimated),
        "epsilon_m": epsilon_m,
        "claim_boundary": (
            "Display decimation only thins drawn samples; raw bridge "
            "trajectory samples remain in bridge_responses unchanged."
        ),
    }


def _build_turtlebot3_indoor_map_model(
    *,
    proposal: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    segment_results: list[dict[str, Any]],
    recovery_segment_result: Mapping[str, Any],
    status: str,
    obstacle_required: bool,
    obstacle: Mapping[str, Any],
    motion: Mapping[str, Any],
    runtime_recovery_triggered: bool,
    recovery_action_suggested: str | None,
) -> dict[str, Any]:
    indoor_route = proposal.get("indoor_delivery_route")
    destination_room_label = (
        str(indoor_route.get("destination_room_label") or "")
        if isinstance(indoor_route, Mapping)
        else ""
    )
    planned_points = [
        _indoor_map_point_from_goal(
            _profile_home_pose(),
            role="home",
            source="missionos_turtlebot3_home_pose",
            sequence=0,
        )
    ]
    for index, goal in enumerate(goals, start=1):
        role = "dropoff" if index == len(goals) else "checkpoint"
        point = _indoor_map_point_from_goal(
            goal,
            role=role,
            source="missionos_planned_nav2_segment",
            sequence=index,
        )
        if role == "dropoff" and destination_room_label:
            point["room_label"] = destination_room_label
        planned_points.append(point)

    observed_points: list[dict[str, Any]] = []
    for result in segment_results:
        observed_points.extend(_observed_points_from_action_result(result))
    recovery_points = _observed_points_from_action_result(recovery_segment_result)
    display_alignment = _observed_display_alignment(
        planned_points=planned_points,
        observed_points=observed_points,
        recovery_points=recovery_points,
    )
    observed_points = _apply_observed_display_alignment(
        observed_points,
        alignment=display_alignment,
    )
    recovery_points = _apply_observed_display_alignment(
        recovery_points,
        alignment=display_alignment,
    )
    observed_points, display_sanitize = _sanitize_observed_display_points(
        observed_points
    )
    recovery_points, _recovery_sanitize = _sanitize_observed_display_points(
        recovery_points
    )
    observed_points, display_decimation = _decimate_observed_display_points(
        observed_points
    )
    display_decimation = {**display_decimation, "sanitize": display_sanitize}
    recovery_points, _recovery_decimation = _decimate_observed_display_points(
        recovery_points
    )
    recovery_goal = recovery_segment_result.get("goal_pose")
    recovery_target = None
    if isinstance(recovery_goal, Mapping):
        recovery_role = (
            "recovery_avoid_obstacle_target"
            if recovery_action_suggested == "avoid_obstacle"
            else "recovery_return_home_target"
        )
        recovery_target = {
            "x_m": recovery_goal.get("x_m"),
            "y_m": recovery_goal.get("y_m"),
            "yaw_rad": recovery_goal.get("yaw_rad"),
            "frame_id": recovery_goal.get("frame_id"),
            "label": recovery_goal.get("label"),
            "role": recovery_role,
            "source": "missionos_recovery_nav2_segment",
        }

    robot_profile = _robot_profile_from_proposal(proposal)
    profile_spec = _robot_profile_spec(robot_profile)
    obstacles = _turtlebot3_delivery_obstacle_markers(
        obstacle_required=obstacle_required,
        obstacle=obstacle,
    )
    floor_plan = _turtlebot3_home_floor_plan(robot_profile)
    floor_bounds = floor_plan["bounds"]

    all_x = [
        float(point["x_m"])
        for point in [*planned_points, *observed_points, *recovery_points, *obstacles]
        if isinstance(point.get("x_m"), (int, float))
        and not isinstance(point.get("x_m"), bool)
    ]
    all_y = [
        float(point["y_m"])
        for point in [*planned_points, *observed_points, *recovery_points, *obstacles]
        if isinstance(point.get("y_m"), (int, float))
        and not isinstance(point.get("y_m"), bool)
    ]
    min_x = min(float(floor_bounds["min_x_m"]), min(all_x, default=-2.5))
    max_x = max(float(floor_bounds["max_x_m"]), max(all_x, default=1.0))
    min_y = min(float(floor_bounds["min_y_m"]), min(all_y, default=-1.0))
    max_y = max(float(floor_bounds["max_y_m"]), max(all_y, default=1.0))
    current_pose = observed_points[-1] if observed_points else None
    if current_pose is None and recovery_points:
        current_pose = recovery_points[-1]
    return {
        "schema_version": TURTLEBOT3_INDOOR_MAP_MODEL_SCHEMA,
        "map_kind": "indoor_local_xy",
        "map_name": profile_spec["map_name"],
        "robot_profile": robot_profile,
        "robot_label": profile_spec["robot_label"],
        "execution_target": profile_spec["execution_target"],
        "runtime_substrate": profile_spec["runtime_substrate"],
        "runtime_profile": profile_spec["runtime_profile"],
        "execution_mode": "sim",
        "robot_model": str(proposal.get("robot_model") or profile_spec["robot_model"]),
        "mission_kind": str(proposal.get("mission_kind") or ""),
        "mission_status": status,
        "frame_id": "map",
        "coordinate_system": {
            "units": "meters",
            "x_axis": "ROS map x",
            "y_axis": "ROS map y",
            "source": (
                "Nav2 map frame; bridge local-XY trajectory samples are display-"
                "aligned to the planned home pose when needed"
            ),
        },
        "display_alignment": display_alignment,
        "display_decimation": display_decimation,
        "room_boundary": {
            "source": "missionos_static_simulated_home_floor_plan_bounds",
            "min_x_m": round(min_x, 3),
            "max_x_m": round(max_x, 3),
            "min_y_m": round(min_y, 3),
            "max_y_m": round(max_y, 3),
            "claim_boundary": (
                "This is a static simulated-home display boundary plus "
                "source-backed MissionOS/Nav2 evidence, not a verified real-home "
                "floor plan."
            ),
        },
        "floor_plan": floor_plan,
        "planned_points": planned_points,
        "observed_points": observed_points,
        "observed_pose_source": "ros2_nav2_bridge_trajectory_samples"
        if observed_points
        else "not_available",
        "current_pose": current_pose,
        "obstacles": obstacles,
        "recovery": {
            "triggered": runtime_recovery_triggered,
            "selected_action": recovery_action_suggested,
            "target": recovery_target,
            "observed_points": recovery_points,
            "completion_claimed": recovery_segment_result.get("completion_claimed") is True,
        },
        "motion": dict(motion),
        "claim_boundaries": [
            "indoor map is read-only evidence display",
            "sim_action is not physical execution",
            "observed trajectory requires bridge-provided pose samples",
            "room boundary is a display envelope unless a source map is attached",
        ],
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
    }


def _dispatch_nav2_goal(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    goal: Nav2GoalPose,
    approval_ref: str,
    dispatched_at: datetime,
    action_ref_suffix: str,
    publish_initialpose: bool,
) -> dict[str, Any]:
    config = Ros2Nav2HardwareAdapterConfig(
        missionos_action_ref=(
            f"{proposal.get('proposal_id') or 'turtlebot3_home_mission'}:"
            f"{action_ref_suffix}"
        ),
        goal_pose=goal,
        execution_mode=HardwareExecutionMode.SIM,
        operator_approval_ref=approval_ref or None,
        approval_actor=str(approval.get("approval_actor") or "missionos_chat_operator"),
        approval_timestamp=dispatched_at,
        max_distance_m=goal.max_distance_m,
        raw_logs_ref=_turtlebot3_raw_logs_ref_from_env(
            _robot_profile_from_proposal(proposal)
        ),
    )
    env_overrides = None if publish_initialpose else {"ROS2_NAV2_INITIALPOSE_ENABLE": "0"}
    client = Ros2Nav2BridgeCommandClient(env_overrides=env_overrides)
    adapter = Ros2Nav2HardwareAdapter(config=config, client=client)
    bridge_error = ""
    try:
        evidence = adapter.dispatch_approved_action()
        bridge_responses = client.collect_responses()
    except Ros2Nav2BridgeError as exc:
        bridge_error = str(exc)
        evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
            config=config,
            blocking_reasons=(
                "ros2_nav2_bridge_receipt_unavailable",
                "ros2_nav2_bridge_error",
            ),
        )
        bridge_responses = ()
    motion = _robot_motion_from_responses(bridge_responses)
    obstacle = _obstacle_observation_from_responses(bridge_responses)
    return {
        "segment_ref": action_ref_suffix,
        "goal_pose": goal.model_dump(mode="json"),
        "publish_initialpose": publish_initialpose,
        "adapter_evidence": evidence.model_dump(mode="json"),
        "bridge_responses": [dict(response) for response in bridge_responses],
        "bridge_error": bridge_error,
        "dispatch_request_sent": evidence.dispatch_request_sent,
        "completion_claimed": evidence.completion_claimed,
        "completion_scope": evidence.completion_scope
        if evidence.completion_claimed
        else "none",
        "blocking_reasons": list(evidence.blocking_reasons),
        "unproven_claims": list(evidence.unproven_claims),
        **motion,
        **obstacle,
    }


def _sum_numeric(values: list[Any]) -> float | None:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numbers:
        return None
    return sum(numbers)


_TURTLEBOT3_RUNTIME_MOTION_STALL_THRESHOLD_M = 0.05


def _planned_route_distance_through_segment(
    goals: tuple[Nav2GoalPose, ...],
    *,
    completed_segment_count: int,
) -> float:
    if completed_segment_count <= 0:
        return 0.0
    distance = 0.0
    previous = _profile_home_pose()
    for goal in goals[:completed_segment_count]:
        distance += math.hypot(goal.x_m - previous.x_m, goal.y_m - previous.y_m)
        previous = goal
    return distance


def _planned_segment_distance_m(
    goals: tuple[Nav2GoalPose, ...],
    *,
    segment_index: int,
) -> float | None:
    if segment_index < 1 or segment_index > len(goals):
        return None
    previous = _profile_home_pose() if segment_index == 1 else goals[segment_index - 2]
    goal = goals[segment_index - 1]
    return math.hypot(goal.x_m - previous.x_m, goal.y_m - previous.y_m)


def _runtime_motion_context(
    *,
    action_result: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    segment_index: int,
    completed_segment_count: int,
) -> dict[str, Any]:
    odom_delta = action_result.get("odom_delta_m")
    odom_delta_m = (
        float(odom_delta)
        if isinstance(odom_delta, (int, float)) and not isinstance(odom_delta, bool)
        else None
    )
    robot_motion_observed = action_result.get("robot_motion_observed") is True
    completion_claimed = action_result.get("completion_claimed") is True
    dispatch_sent = action_result.get("dispatch_request_sent") is True
    stalled_after_dispatch = (
        dispatch_sent
        and not completion_claimed
        and (
            odom_delta_m is None
            or odom_delta_m <= _TURTLEBOT3_RUNTIME_MOTION_STALL_THRESHOLD_M
            or not robot_motion_observed
        )
    )
    context = {
        "schema_version": "missionos_turtlebot3_runtime_motion_context.v1",
        "robot_motion_observed": robot_motion_observed,
        "odom_delta_m": odom_delta_m,
        "odom_topic": action_result.get("odom_topic"),
        "motion_observation_source": action_result.get(
            "robot_motion_observation_source"
        )
        or "not_available",
        "route_progress_delta_m": round(
            _planned_route_distance_through_segment(
                goals,
                completed_segment_count=completed_segment_count,
            ),
            3,
        ),
        "completed_route_distance_m": round(
            _planned_route_distance_through_segment(
                goals,
                completed_segment_count=completed_segment_count,
            ),
            3,
        ),
        "planned_segment_distance_m": (
            round(segment_distance, 3)
            if (
                segment_distance := _planned_segment_distance_m(
                    goals,
                    segment_index=segment_index,
                )
            )
            is not None
            else None
        ),
        "stalled_after_dispatch": stalled_after_dispatch,
        "motion_stall_threshold_m": _TURTLEBOT3_RUNTIME_MOTION_STALL_THRESHOLD_M,
        "telemetry_window_ref": action_result.get("telemetry_window_ref"),
        "source": "ros2_nav2_action_result_motion",
        "claim_boundary": (
            "Runtime motion context is source-backed telemetry for LLM recovery "
            "judgment only; it does not approve, dispatch, or claim completion."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return {key: value for key, value in context.items() if value is not None}


def _max_numeric(values: list[Any]) -> float | None:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numbers:
        return None
    return max(numbers)


def run_turtlebot3_home_mission_dispatch(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    now: datetime | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute an approved TurtleBot3 home mission through the Nav2 bridge.

    ``progress_callback`` receives a partial, claim-safe running summary after
    each segment so live surfaces (map/watch/operate) can show the mission
    advancing; partial payloads never claim completion.
    """

    dispatched_at = now or datetime.now(timezone.utc)
    goals = _planned_segment_goals_from_proposal(proposal)
    goal = goals[-1]
    robot_profile = _robot_profile_from_proposal(proposal)
    profile_spec = _robot_profile_spec(robot_profile)
    approval_ref = str(approval.get("operator_approval_ref") or "").strip()
    autonomy_envelope = approval.get("autonomy_envelope")
    if not isinstance(autonomy_envelope, Mapping):
        autonomy_envelope = proposal.get("autonomy_envelope")
    autonomy_envelope = dict(autonomy_envelope) if isinstance(autonomy_envelope, Mapping) else {}
    battery_envelope = proposal.get("battery_envelope")
    battery_envelope = (
        dict(battery_envelope)
        if isinstance(battery_envelope, Mapping)
        else {}
    )
    recovery_proposals = tuple(
        dict(item)
        for item in proposal.get("recovery_proposals") or ()
        if isinstance(item, Mapping)
    )
    recovery_planner_result = proposal.get("recovery_planner_result")
    recovery_planner_result = (
        dict(recovery_planner_result)
        if isinstance(recovery_planner_result, Mapping)
        else {}
    )
    home_distance_envelope = proposal.get("home_distance_envelope")
    home_distance_envelope = (
        dict(home_distance_envelope)
        if isinstance(home_distance_envelope, Mapping)
        else {}
    )
    recovery_proposal_classifications = (
        _classify_recovery_proposals(
            autonomy_envelope=autonomy_envelope,
            recovery_proposals=recovery_proposals,
        )
        if autonomy_envelope
        else ()
    )
    recovery_action_suggested = (
        str(recovery_proposals[0].get("selected_action"))
        if recovery_proposals
        else None
    )
    recovery_execution_permitted_by_envelope = any(
        classification.get("execution_permitted_by_envelope") is True
        for classification in recovery_proposal_classifications
    )
    recovery_execution_permitted_by_operator_approval = False
    recovery_dispatch_authority_source: str | None = (
        "autonomy_envelope" if recovery_execution_permitted_by_envelope else None
    )
    fresh_recovery_operator_approvals: list[dict[str, Any]] = []

    blocking_reasons = list(_bridge_readiness_blocking_reasons(robot_profile))
    blocking_reasons.extend(_pre_dispatch_judgment_blocking_reasons(proposal))
    if not approval_ref or approval.get("operator_approved") is not True:
        blocking_reasons.append("operator_approval_missing")

    segment_results: list[dict[str, Any]] = []
    recovery_segment_result: dict[str, Any] = {}

    def _emit_progress(
        *,
        runtime_recovery_triggered: bool = False,
        recovery_action_suggested: str | None = None,
    ) -> None:
        if progress_callback is None:
            return
        try:
            partial_map = _build_turtlebot3_indoor_map_model(
                proposal=proposal,
                goals=goals,
                segment_results=segment_results,
                recovery_segment_result=recovery_segment_result,
                status="running",
                obstacle_required=_obstacle_challenge_required(proposal),
                obstacle={},
                motion={},
                runtime_recovery_triggered=runtime_recovery_triggered,
                recovery_action_suggested=recovery_action_suggested,
            )
            progress_callback(
                {
                    "summary": {
                        "status": "running",
                        "planned_segment_count": len(goals),
                        "segment_dispatch_count": len(segment_results),
                        "segment_completion_count": sum(
                            1
                            for item in segment_results
                            if item.get("completion_claimed") is True
                        ),
                        "runtime_recovery_triggered": runtime_recovery_triggered,
                        "completion_claimed": False,
                        "completion_scope": "none",
                        "physical_execution_invoked": False,
                        "mission_delivery_completion_claimed": False,
                        "turtlebot3_indoor_map_model": partial_map,
                    },
                }
            )
        except Exception:
            # Live progress is best-effort display only; it must never break
            # or alter the dispatch loop.
            pass

    runtime_recovery_triggered = False
    runtime_recovery_battery_envelope: dict[str, Any] = {}
    runtime_recovery_home_distance_envelope: dict[str, Any] = {}
    runtime_recovery_obstacle_scenario: dict[str, Any] = {}
    runtime_recovery_motion_context: dict[str, Any] = {}
    runtime_recovery_action_kind: str | None = None
    runtime_failure_context: dict[str, Any] = {}
    route_resumed_after_recovery = False
    if blocking_reasons:
        config = Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref=str(
                proposal.get("proposal_id") or "turtlebot3_home_mission"
            ),
            goal_pose=goal,
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref=approval_ref or None,
            approval_actor=str(approval.get("approval_actor") or "missionos_chat_operator"),
            approval_timestamp=dispatched_at,
            max_distance_m=goal.max_distance_m,
            raw_logs_ref=_turtlebot3_raw_logs_ref_from_env(robot_profile),
        )
        evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
            config=config,
            blocking_reasons=tuple(dict.fromkeys(blocking_reasons)),
        )
    else:
        evidence = None
        trigger_after = battery_envelope.get("runtime_recovery_trigger_after_segment_index")
        obstacle_scenario = (
            proposal.get("obstacle_scenario")
            if isinstance(proposal.get("obstacle_scenario"), Mapping)
            else {}
        )
        obstacle_trigger_after = obstacle_scenario.get(
            "runtime_obstacle_recovery_trigger_after_segment_index"
        )
        for index, segment_goal in enumerate(goals, start=1):
            result = _dispatch_nav2_goal(
                proposal=proposal,
                approval=approval,
                goal=segment_goal,
                approval_ref=approval_ref,
                dispatched_at=dispatched_at,
                action_ref_suffix=f"segment_{index}",
                publish_initialpose=index == 1,
            )
            segment_results.append(result)
            _emit_progress(
                runtime_recovery_triggered=runtime_recovery_triggered,
                recovery_action_suggested=runtime_recovery_action_kind,
            )
            evidence = result["adapter_evidence"]
            if result["completion_claimed"] is not True:
                # A real (unplanned) segment failure convenes the same recovery
                # machinery as the scripted battery/obstacle triggers: the
                # planner (LLM with guardrails, deterministic floor as
                # fallback) proposes, the autonomy envelope classifies, and
                # only an envelope-permitted return_home is dispatched
                # immediately under the existing mission approval. Any other
                # proposal is attached to the blocked result for the operator.
                runtime_recovery_triggered = True
                runtime_failure_context = {
                    "schema_version": (
                        "missionos_turtlebot3_runtime_segment_failure.v1"
                    ),
                    "runtime_failure_observed": True,
                    "failed_segment_index": index,
                    "failed_segment_label": segment_goal.label,
                    "failed_segment_blocking_reasons": list(
                        result.get("blocking_reasons") or ()
                    ),
                    "failed_segment_blocking_reason_count": len(
                        result.get("blocking_reasons") or ()
                    ),
                    "failed_segment_completion_claimed": False,
                    "source": "ros2_nav2_bridge_segment_result",
                    "runtime_failure_source": "ros2_nav2_bridge_segment_result",
                    "recommended_recovery_action": "return_home",
                }
                runtime_recovery_battery_envelope = (
                    _runtime_recovery_battery_envelope(battery_envelope)
                )
                runtime_recovery_home_distance_envelope = (
                    _build_home_distance_envelope("", segment_goal)
                )
                runtime_recovery_home_distance_envelope[
                    "distance_to_home_source"
                ] = "runtime_segment_goal_projection"
                runtime_recovery_home_distance_envelope["runtime_observed"] = False
                runtime_recovery_motion_context = _runtime_motion_context(
                    action_result=result,
                    goals=goals,
                    segment_index=index,
                    completed_segment_count=sum(
                        1
                        for item in segment_results
                        if item.get("completion_claimed") is True
                    ),
                )
                (
                    runtime_recovery_proposals,
                    runtime_recovery_planner_result,
                ) = _build_recovery_proposals(
                    proposal_id=str(
                        proposal.get("proposal_id") or "turtlebot3_home_mission"
                    ),
                    operator_instruction=str(
                        proposal.get("operator_instruction") or ""
                    ),
                    battery_envelope=runtime_recovery_battery_envelope,
                    home_distance_envelope=runtime_recovery_home_distance_envelope,
                    autonomy_envelope=autonomy_envelope,
                    obstacle_scenario=proposal.get("obstacle_scenario")
                    if isinstance(proposal.get("obstacle_scenario"), Mapping)
                    else {},
                    indoor_delivery_route=proposal.get("indoor_delivery_route")
                    if isinstance(proposal.get("indoor_delivery_route"), Mapping)
                    else {},
                    runtime_failure_context=runtime_failure_context,
                    runtime_motion_context=runtime_recovery_motion_context,
                    runtime_observation_phase=True,
                )
                recovery_proposals = tuple(
                    item.model_dump(mode="json")
                    for item in runtime_recovery_proposals
                )
                recovery_planner_result = dict(runtime_recovery_planner_result)
                recovery_proposal_classifications = _classify_recovery_proposals(
                    autonomy_envelope=autonomy_envelope,
                    recovery_proposals=recovery_proposals,
                )
                recovery_action_suggested = (
                    str(recovery_proposals[0].get("selected_action"))
                    if recovery_proposals
                    else None
                )
                runtime_recovery_action_kind = recovery_action_suggested
                recovery_execution_permitted_by_envelope = any(
                    classification.get("execution_permitted_by_envelope") is True
                    for classification in recovery_proposal_classifications
                )
                if (
                    recovery_action_suggested == "return_home"
                    and recovery_execution_permitted_by_envelope
                ):
                    recovery_segment_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=_profile_home_pose(),
                        approval_ref=approval_ref,
                        dispatched_at=dispatched_at,
                        action_ref_suffix="recovery_return_home_after_failure",
                        publish_initialpose=False,
                    )
                    evidence = recovery_segment_result["adapter_evidence"]
                _emit_progress(
                    runtime_recovery_triggered=True,
                    recovery_action_suggested=runtime_recovery_action_kind,
                )
                break
            if (
                trigger_after == index
                and battery_envelope.get("runtime_recovery_required") is True
            ):
                runtime_recovery_triggered = True
                runtime_recovery_action_kind = "return_home"
                runtime_recovery_battery_envelope = _runtime_recovery_battery_envelope(
                    battery_envelope
                )
                runtime_recovery_home_distance_envelope = _build_home_distance_envelope(
                    "",
                    segment_goal,
                )
                runtime_recovery_home_distance_envelope[
                    "distance_to_home_source"
                ] = "runtime_segment_goal_projection"
                runtime_recovery_home_distance_envelope[
                    "runtime_observed"
                ] = False
                runtime_recovery_motion_context = _runtime_motion_context(
                    action_result=result,
                    goals=goals,
                    segment_index=index,
                    completed_segment_count=sum(
                        1
                        for item in segment_results
                        if item.get("completion_claimed") is True
                    ),
                )
                (
                    runtime_recovery_proposals,
                    runtime_recovery_planner_result,
                ) = _build_recovery_proposals(
                    proposal_id=str(
                        proposal.get("proposal_id") or "turtlebot3_home_mission"
                    ),
                    operator_instruction=str(proposal.get("operator_instruction") or ""),
                    battery_envelope=runtime_recovery_battery_envelope,
                    home_distance_envelope=runtime_recovery_home_distance_envelope,
                    autonomy_envelope=autonomy_envelope,
                    obstacle_scenario=proposal.get("obstacle_scenario")
                    if isinstance(proposal.get("obstacle_scenario"), Mapping)
                    else {},
                    indoor_delivery_route=proposal.get("indoor_delivery_route")
                    if isinstance(proposal.get("indoor_delivery_route"), Mapping)
                    else {},
                    runtime_motion_context=runtime_recovery_motion_context,
                )
                recovery_proposals = tuple(
                    item.model_dump(mode="json")
                    for item in runtime_recovery_proposals
                )
                recovery_planner_result = dict(runtime_recovery_planner_result)
                recovery_proposal_classifications = _classify_recovery_proposals(
                    autonomy_envelope=autonomy_envelope,
                    recovery_proposals=recovery_proposals,
                )
                recovery_action_suggested = (
                    str(recovery_proposals[0].get("selected_action"))
                    if recovery_proposals
                    else None
                )
                recovery_execution_permitted_by_envelope = any(
                    classification.get("execution_permitted_by_envelope") is True
                    for classification in recovery_proposal_classifications
                )
                if (
                    recovery_action_suggested == "return_home"
                    and recovery_execution_permitted_by_envelope
                ):
                    recovery_segment_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=_profile_home_pose(),
                        approval_ref=approval_ref,
                        dispatched_at=dispatched_at,
                        action_ref_suffix="recovery_return_home",
                        publish_initialpose=False,
                    )
                    evidence = recovery_segment_result["adapter_evidence"]
                    _emit_progress(
                        runtime_recovery_triggered=True,
                        recovery_action_suggested=runtime_recovery_action_kind,
                    )
                break
            if (
                obstacle_trigger_after == index
                and obstacle_scenario.get("runtime_obstacle_recovery_requested") is True
            ):
                runtime_recovery_triggered = True
                runtime_recovery_action_kind = "avoid_obstacle"
                runtime_recovery_obstacle_scenario = _runtime_recovery_obstacle_scenario(
                    obstacle_scenario,
                    segment_result=result,
                )
                runtime_recovery_motion_context = _runtime_motion_context(
                    action_result=result,
                    goals=goals,
                    segment_index=index,
                    completed_segment_count=sum(
                        1
                        for item in segment_results
                        if item.get("completion_claimed") is True
                    ),
                )
                (
                    runtime_recovery_proposals,
                    runtime_recovery_planner_result,
                ) = _build_recovery_proposals(
                    proposal_id=str(
                        proposal.get("proposal_id") or "turtlebot3_home_mission"
                    ),
                    operator_instruction=str(proposal.get("operator_instruction") or ""),
                    battery_envelope=battery_envelope,
                    home_distance_envelope=home_distance_envelope,
                    autonomy_envelope=autonomy_envelope,
                    obstacle_scenario=runtime_recovery_obstacle_scenario,
                    indoor_delivery_route=proposal.get("indoor_delivery_route")
                    if isinstance(proposal.get("indoor_delivery_route"), Mapping)
                    else {},
                    runtime_motion_context=runtime_recovery_motion_context,
                    runtime_observation_phase=True,
                )
                recovery_proposals = tuple(
                    item.model_dump(mode="json")
                    for item in runtime_recovery_proposals
                )
                recovery_planner_result = dict(runtime_recovery_planner_result)
                recovery_proposal_classifications = _classify_recovery_proposals(
                    autonomy_envelope=autonomy_envelope,
                    recovery_proposals=recovery_proposals,
                )
                recovery_action_suggested = (
                    str(recovery_proposals[0].get("selected_action"))
                    if recovery_proposals
                    else None
                )
                recovery_execution_permitted_by_envelope = any(
                    classification.get("execution_permitted_by_envelope") is True
                    for classification in recovery_proposal_classifications
                )
                fresh_approval = _fresh_recovery_operator_approval_from_env(
                    selected_action=recovery_action_suggested,
                    recovery_proposals=recovery_proposals,
                    recovery_proposal_classifications=recovery_proposal_classifications,
                    approved_at=dispatched_at,
                )
                if fresh_approval:
                    fresh_recovery_operator_approvals.append(fresh_approval)
                    recovery_execution_permitted_by_operator_approval = True
                recovery_dispatch_authority_source = (
                    "autonomy_envelope"
                    if recovery_execution_permitted_by_envelope
                    else "fresh_operator_approval"
                    if recovery_execution_permitted_by_operator_approval
                    else None
                )
                if (
                    recovery_action_suggested == "avoid_obstacle"
                    and (
                        recovery_execution_permitted_by_envelope
                        or recovery_execution_permitted_by_operator_approval
                    )
                ):
                    recovery_approval_ref = (
                        fresh_recovery_operator_approvals[-1]["operator_approval_ref"]
                        if recovery_execution_permitted_by_operator_approval
                        else approval_ref
                    )
                    recovery_segment_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=_profile_dynamic_obstacle_avoidance_goal(),
                        approval_ref=recovery_approval_ref,
                        dispatched_at=dispatched_at,
                        action_ref_suffix="recovery_avoid_obstacle",
                        publish_initialpose=False,
                    )
                    evidence = recovery_segment_result["adapter_evidence"]
                    _emit_progress(
                        runtime_recovery_triggered=True,
                        recovery_action_suggested="avoid_obstacle",
                    )
                    route_resumed_after_recovery = (
                        recovery_segment_result.get("completion_claimed") is True
                    )
                    if not route_resumed_after_recovery:
                        break
                else:
                    break
        if evidence is None:
            config = Ros2Nav2HardwareAdapterConfig(
                missionos_action_ref=str(
                    proposal.get("proposal_id") or "turtlebot3_home_mission"
                ),
                goal_pose=goal,
                execution_mode=HardwareExecutionMode.SIM,
                operator_approval_ref=approval_ref or None,
                approval_actor=str(
                    approval.get("approval_actor") or "missionos_chat_operator"
                ),
                approval_timestamp=dispatched_at,
                max_distance_m=goal.max_distance_m,
                raw_logs_ref=_turtlebot3_raw_logs_ref_from_env(robot_profile),
            )
            evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
                config=config,
                blocking_reasons=("no_nav2_segments_to_dispatch",),
            )

    evidence_payload = (
        evidence.model_dump(mode="json")
        if hasattr(evidence, "model_dump")
        else dict(evidence)
    )
    all_action_results = [
        *segment_results,
        *([recovery_segment_result] if recovery_segment_result else []),
    ]
    bridge_responses = tuple(
        response
        for result in all_action_results
        for response in result.get("bridge_responses") or ()
        if isinstance(response, Mapping)
    )
    bridge_error = "; ".join(
        str(result.get("bridge_error"))
        for result in all_action_results
        if result.get("bridge_error")
    )
    main_dispatch_sent = any(result.get("dispatch_request_sent") is True for result in segment_results)
    all_route_segments_completed = (
        bool(segment_results)
        and len(segment_results) == len(goals)
        and all(result.get("completion_claimed") is True for result in segment_results)
    )
    main_segments_completed = (
        all_route_segments_completed
        and not runtime_recovery_triggered
    )
    route_completed_after_recovery = (
        all_route_segments_completed
        and runtime_recovery_triggered
        and runtime_recovery_action_kind == "avoid_obstacle"
        and route_resumed_after_recovery
    )
    recovery_dispatch_request_sent = (
        recovery_segment_result.get("dispatch_request_sent") is True
    )
    recovery_completion_claimed = (
        recovery_segment_result.get("completion_claimed") is True
    )
    motion = {
        "robot_motion_observed": any(
            result.get("robot_motion_observed") is True for result in all_action_results
        ),
        "odom_delta_m": _sum_numeric(
            [result.get("odom_delta_m") for result in all_action_results]
        ),
        "odom_topic": next(
            (
                result.get("odom_topic")
                for result in all_action_results
                if result.get("odom_topic")
            ),
            None,
        ),
        "robot_motion_observation_source": "ros2_nav2_bridge_receipt"
        if all_action_results
        else "not_available",
    }
    (
        telemetry_sidecar_artifacts,
        motion,
        telemetry_sidecar_blocking_reasons,
        telemetry_sidecar_required,
    ) = _turtlebot3_sidecar_motion_artifacts(bridge_motion=motion)
    obstacle = {
        "obstacle_detected": any(
            result.get("obstacle_detected") is True for result in all_action_results
        ),
        "costmap_obstacle_observed": any(
            result.get("costmap_obstacle_observed") is True
            for result in all_action_results
        ),
        "obstacle_avoidance_observed": any(
            result.get("obstacle_avoidance_observed") is True
            for result in all_action_results
        ),
        "trajectory_lateral_deviation_observed": any(
            result.get("trajectory_lateral_deviation_observed") is True
            for result in all_action_results
        ),
        "max_lateral_deviation_m": _max_numeric(
            [result.get("max_lateral_deviation_m") for result in all_action_results]
        ),
        "avoidance_observation_source": "ros2_nav2_bridge_costmap_and_odom"
        if any(
            result.get("obstacle_avoidance_observed") is True
            for result in all_action_results
        )
        else None,
    }
    nvblox_evidence = build_nvblox_perception_evidence_from_env_or_responses(
        bridge_responses
    )
    nvblox_evidence_payload = (
        nvblox_evidence.model_dump(mode="json")
        if nvblox_evidence.evidence_status != "not_requested"
        else {}
    )
    nvblox_perception_supports_costmap = (
        nvblox_evidence_payload.get("nav2_costmap_updated_from_perception") is True
    )
    if nvblox_perception_supports_costmap:
        obstacle["obstacle_detected"] = True
        obstacle["costmap_obstacle_observed"] = True
        obstacle["avoidance_observation_source"] = (
            obstacle["avoidance_observation_source"]
            or "isaac_ros_nvblox_perception_evidence"
        )
    if nvblox_evidence_payload.get("dynamic_obstacle_observed") is True:
        obstacle["obstacle_detected"] = True
    mission_kind = str(proposal.get("mission_kind") or "")
    obstacle_required = _obstacle_challenge_required(proposal)
    obstacle_geometry = _obstacle_trajectory_geometry(
        obstacle_required=obstacle_required,
        obstacle=obstacle,
        planned_points=_planned_indoor_map_points(goals),
        observed_points=[
            point
            for result in segment_results
            for point in _observed_points_from_action_result(result)
        ],
        recovery_points=_observed_points_from_action_result(recovery_segment_result),
    )
    bridge_obstacle_avoidance_observed = obstacle["obstacle_avoidance_observed"]
    obstacle.update(obstacle_geometry)
    obstacle["bridge_obstacle_avoidance_observed"] = bridge_obstacle_avoidance_observed
    if obstacle_required:
        obstacle["obstacle_avoidance_observed"] = (
            bridge_obstacle_avoidance_observed is True
            and obstacle_geometry["obstacle_trajectory_clearance_observed"] is True
        )
    delivery_route_requested = mission_kind == "indoor_delivery_route_leg"
    obstacle_avoidance_completion_claimed = (
        (main_segments_completed or route_completed_after_recovery)
        and obstacle_required
        and obstacle["obstacle_avoidance_observed"] is True
    )
    telemetry_sidecar_motion_confirmed = (
        not telemetry_sidecar_required
        or not telemetry_sidecar_blocking_reasons
    )
    route_completion_candidate = main_segments_completed or route_completed_after_recovery
    mission_completion_claimed = (
        route_completion_candidate
        and (not obstacle_required or obstacle_avoidance_completion_claimed)
        and telemetry_sidecar_motion_confirmed
    )
    mission_blocking_reasons = list(evidence_payload.get("blocking_reasons") or [])
    mission_blocking_reasons.extend(
        str(reason)
        for reason in nvblox_evidence_payload.get("blocking_reasons") or []
    )
    if (
        route_completion_candidate
        and obstacle_required
        and not obstacle_avoidance_completion_claimed
    ):
        mission_blocking_reasons.append("obstacle_avoidance_not_observed")
        if obstacle.get("obstacle_trajectory_intersects_obstacle") is True:
            mission_blocking_reasons.append("obstacle_trajectory_intersects_obstacle")
    if route_completion_candidate and telemetry_sidecar_required:
        if not telemetry_sidecar_motion_confirmed:
            mission_blocking_reasons.append(
                "telemetry_sidecar_motion_correlation_not_confirmed"
            )
        mission_blocking_reasons.extend(telemetry_sidecar_blocking_reasons)
    status = (
        "completed"
        if mission_completion_claimed
        else "recovered"
        if runtime_recovery_triggered and recovery_completion_claimed
        else "blocked"
        if mission_blocking_reasons or not main_dispatch_sent
        else "incomplete"
    )
    runtime_configuration_status = _runtime_configuration_status(
        mission_blocking_reasons
    )
    indoor_delivery_route_completion_claimed = (
        mission_completion_claimed and delivery_route_requested
    )
    indoor_map_model = _build_turtlebot3_indoor_map_model(
        proposal=proposal,
        goals=goals,
        segment_results=segment_results,
        recovery_segment_result=recovery_segment_result,
        status=status,
        obstacle_required=obstacle_required,
        obstacle=obstacle,
        motion=motion,
        runtime_recovery_triggered=runtime_recovery_triggered,
        recovery_action_suggested=recovery_action_suggested,
    )
    log_bundle_artifacts = _turtlebot3_log_bundle_artifacts(robot_profile)
    raw_logs_ref = log_bundle_artifacts.get("raw_logs_ref") or motion.get(
        "telemetry_raw_logs_ref"
    )
    planned_route_distance = _planned_route_distance_m(goals)
    execution = {
        "schema_version": TURTLEBOT3_HOME_MISSION_EXECUTION_SCHEMA,
        "status": status,
        "mission_kind": mission_kind,
        "robot_profile": robot_profile,
        "robot_label": profile_spec["robot_label"],
        "robot_model": profile_spec["robot_model"],
        "execution_target": profile_spec["execution_target"],
        "runtime_substrate": profile_spec["runtime_substrate"],
        "runtime_profile": profile_spec["runtime_profile"],
        "runtime_configuration_status": runtime_configuration_status,
        "execution_mode": "sim",
        "nav2_goal_pose": goal.model_dump(mode="json"),
        "planned_segments": [item.model_dump(mode="json") for item in goals],
        "planned_route_distance_m": planned_route_distance,
        "segment_results": [dict(item) for item in segment_results],
        "adapter_evidence": dict(evidence_payload),
        "adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {}) for item in segment_results
        ],
        "recovery_segment_result": dict(recovery_segment_result),
        "bridge_responses": [dict(response) for response in bridge_responses],
        "bridge_error": bridge_error,
        "turtlebot3_indoor_map_model": dict(indoor_map_model),
        "log_bundle_artifacts": dict(log_bundle_artifacts),
        "raw_logs_ref": raw_logs_ref,
        "telemetry_sidecar_artifacts": dict(telemetry_sidecar_artifacts),
        "telemetry_sidecar_required": telemetry_sidecar_required,
        "telemetry_sidecar_motion_correlation_confirmed": (
            telemetry_sidecar_motion_confirmed
        ),
        "telemetry_sidecar_blocking_reasons": list(
            telemetry_sidecar_blocking_reasons
        ),
        "nvblox_perception_evidence": dict(nvblox_evidence_payload),
        "nvblox_perception_evidence_status": nvblox_evidence.evidence_status,
        "nvblox_perception_evidence_available": (
            nvblox_evidence.perception_evidence_available
        ),
        "nvblox_perception_supports_obstacle_claim_only_with_trajectory": (
            nvblox_evidence
            .supports_obstacle_aware_claim_when_paired_with_trajectory_evidence
        ),
        "autonomy_envelope": dict(autonomy_envelope),
        "home_distance_envelope": dict(home_distance_envelope),
        "runtime_recovery_battery_envelope": dict(runtime_recovery_battery_envelope),
        "runtime_recovery_home_distance_envelope": dict(
            runtime_recovery_home_distance_envelope
        ),
        "runtime_recovery_obstacle_scenario": dict(runtime_recovery_obstacle_scenario),
        "runtime_recovery_motion_context": dict(runtime_recovery_motion_context),
        "recovery_planner_result": dict(recovery_planner_result),
        "recovery_planner_status": recovery_planner_result.get("planner_status"),
        "recovery_proposals": [dict(item) for item in recovery_proposals],
        "recovery_proposal_classifications": list(
            recovery_proposal_classifications
        ),
        "fresh_recovery_operator_approvals": [
            dict(item) for item in fresh_recovery_operator_approvals
        ],
        "fresh_recovery_operator_approval_count": len(
            fresh_recovery_operator_approvals
        ),
        "recovery_action_suggested": recovery_action_suggested,
        "recovery_execution_permitted_by_envelope": (
            recovery_execution_permitted_by_envelope
        ),
        "recovery_execution_permitted_by_operator_approval": (
            recovery_execution_permitted_by_operator_approval
        ),
        "recovery_dispatch_authority_source": recovery_dispatch_authority_source,
        "runtime_recovery_triggered": runtime_recovery_triggered,
        "runtime_recovery_action_kind": runtime_recovery_action_kind,
        "runtime_failure_context": dict(runtime_failure_context),
        "runtime_failure_recovery_triggered": bool(runtime_failure_context),
        "route_resumed_after_recovery": route_resumed_after_recovery,
        "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
        "recovery_completion_claimed": recovery_completion_claimed,
        "llm_recovery_proposals_allowed": bool(
            autonomy_envelope.get("llm_recovery_proposals_allowed", True)
        ),
        "proposal_first_classification": bool(
            autonomy_envelope.get("proposal_first_classification", True)
        ),
        "dispatch_request_sent": main_dispatch_sent,
        "completion_claimed": mission_completion_claimed,
        "completion_scope": evidence_payload.get("completion_scope")
        if mission_completion_claimed
        else "none",
        "nav2_action_completion_claimed": route_completion_candidate,
        "planned_segment_count": len(goals),
        "segment_dispatch_count": sum(
            1 for result in segment_results if result.get("dispatch_request_sent") is True
        ),
        "segment_completion_count": sum(
            1 for result in segment_results if result.get("completion_claimed") is True
        ),
        "multi_segment_mission_claimed": len(goals) > 1 and mission_completion_claimed,
        "route_interrupted_for_recovery": runtime_recovery_triggered,
        "route_completed_after_recovery": route_completed_after_recovery,
        "obstacle_challenge_required": obstacle_required,
        "obstacle_avoidance_completion_claimed": obstacle_avoidance_completion_claimed,
        "indoor_delivery_route_completion_claimed": (
            indoor_delivery_route_completion_claimed
        ),
        "dropoff_arrival_claimed": indoor_delivery_route_completion_claimed,
        "patrol_leg_completion_claimed": (
            mission_completion_claimed
            and mission_kind in {"indoor_patrol_leg", "obstacle_avoidance_patrol_leg"}
        ),
        "cleaning_completion_claimed": False,
        "payload_delivery_completion_claimed": False,
        "whole_home_loop_completion_claimed": False,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": bool(evidence_payload.get("physical_execution_invoked")),
        "progress_counted": False,
        **motion,
        **obstacle,
    }
    result = {
        "turtlebot3_home_mission_execution": execution,
        "turtlebot3_indoor_map_model": dict(indoor_map_model),
        "log_bundle_artifacts": dict(log_bundle_artifacts),
        "telemetry_sidecar_artifacts": dict(telemetry_sidecar_artifacts),
        "nvblox_perception_evidence": dict(nvblox_evidence_payload),
        "ros2_nav2_hardware_adapter_evidence": dict(evidence_payload),
        "ros2_nav2_hardware_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {}) for item in segment_results
        ],
        "summary": {
            "status": status,
            "home_robot_mission_kind": mission_kind,
            "robot_profile": robot_profile,
            "robot_label": profile_spec["robot_label"],
            "robot_model": profile_spec["robot_model"],
            "execution_target": profile_spec["execution_target"],
            "runtime_substrate": profile_spec["runtime_substrate"],
            "runtime_profile": profile_spec["runtime_profile"],
            "runtime_configuration_status": runtime_configuration_status,
            "execution_mode": "sim",
            "dispatch_request_sent": main_dispatch_sent,
            "completion_claimed": mission_completion_claimed,
            "completion_scope": evidence_payload.get("completion_scope")
            if mission_completion_claimed
            else "none",
            "autonomy_envelope": dict(autonomy_envelope),
            "home_distance_envelope": dict(home_distance_envelope),
            "runtime_recovery_battery_envelope": dict(runtime_recovery_battery_envelope),
            "runtime_recovery_home_distance_envelope": dict(
                runtime_recovery_home_distance_envelope
            ),
            "runtime_recovery_obstacle_scenario": dict(
                runtime_recovery_obstacle_scenario
            ),
            "runtime_recovery_motion_context": dict(runtime_recovery_motion_context),
            "recovery_planner_result": dict(recovery_planner_result),
            "recovery_planner_status": recovery_planner_result.get("planner_status"),
            "recovery_proposals": [dict(item) for item in recovery_proposals],
            "recovery_proposal_classifications": list(
                recovery_proposal_classifications
            ),
            "fresh_recovery_operator_approvals": [
                dict(item) for item in fresh_recovery_operator_approvals
            ],
            "fresh_recovery_operator_approval_count": len(
                fresh_recovery_operator_approvals
            ),
            "recovery_action_suggested": recovery_action_suggested,
            "recovery_execution_permitted_by_envelope": (
                recovery_execution_permitted_by_envelope
            ),
            "recovery_execution_permitted_by_operator_approval": (
                recovery_execution_permitted_by_operator_approval
            ),
            "recovery_dispatch_authority_source": recovery_dispatch_authority_source,
            "runtime_recovery_triggered": runtime_recovery_triggered,
            "runtime_recovery_action_kind": runtime_recovery_action_kind,
            "runtime_failure_context": dict(runtime_failure_context),
            "runtime_failure_recovery_triggered": bool(runtime_failure_context),
            "route_resumed_after_recovery": route_resumed_after_recovery,
            "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
            "recovery_completion_claimed": recovery_completion_claimed,
            "llm_recovery_proposals_allowed": bool(
                autonomy_envelope.get("llm_recovery_proposals_allowed", True)
            ),
            "proposal_first_classification": bool(
                autonomy_envelope.get("proposal_first_classification", True)
            ),
            "nav2_action_completion_claimed": route_completion_candidate,
            "planned_segments": [item.model_dump(mode="json") for item in goals],
            "planned_segment_count": len(goals),
            "planned_route_distance_m": planned_route_distance,
            "segment_dispatch_count": execution["segment_dispatch_count"],
            "segment_completion_count": execution["segment_completion_count"],
            "multi_segment_mission_claimed": execution["multi_segment_mission_claimed"],
            "route_interrupted_for_recovery": runtime_recovery_triggered,
            "route_completed_after_recovery": route_completed_after_recovery,
            "segment_results": [dict(item) for item in segment_results],
            "recovery_segment_result": dict(recovery_segment_result),
            "turtlebot3_indoor_map_model": dict(indoor_map_model),
            "log_bundle_artifacts": dict(log_bundle_artifacts),
            "log_bundle_status": log_bundle_artifacts.get("log_bundle_status"),
            "raw_logs_ref": raw_logs_ref,
            "log_bundle_observed_source_count": log_bundle_artifacts.get(
                "observed_source_count"
            ),
            "log_bundle_source_count": log_bundle_artifacts.get("source_count"),
            "log_bundle_blocking_reasons": log_bundle_artifacts.get(
                "blocking_reasons"
            )
            or [],
            "nav2_log_diagnostics": log_bundle_artifacts.get(
                "nav2_log_diagnostics"
            )
            or {},
            "nav2_log_diagnostics_status": log_bundle_artifacts.get(
                "nav2_log_diagnostics_status"
            ),
            "nav2_log_observed_patterns": log_bundle_artifacts.get(
                "nav2_log_observed_patterns"
            )
            or [],
            "nav2_log_failure_hypotheses": log_bundle_artifacts.get(
                "nav2_log_failure_hypotheses"
            )
            or [],
            "telemetry_sidecar_artifacts": dict(telemetry_sidecar_artifacts),
            "telemetry_sidecar_required": telemetry_sidecar_required,
            "telemetry_sidecar_motion_correlation_confirmed": (
                telemetry_sidecar_motion_confirmed
            ),
            "telemetry_sidecar_blocking_reasons": list(
                telemetry_sidecar_blocking_reasons
            ),
            "nvblox_perception_evidence": dict(nvblox_evidence_payload),
            "nvblox_perception_evidence_status": nvblox_evidence.evidence_status,
            "nvblox_perception_evidence_available": (
                nvblox_evidence.perception_evidence_available
            ),
            "perception_source": (
                nvblox_evidence.perception_source
                if nvblox_evidence_payload
                else None
            ),
            "depth_input_observed": nvblox_evidence.depth_input_observed,
            "pose_input_observed": nvblox_evidence.pose_input_observed,
            "scene_reconstruction_observed": (
                nvblox_evidence.scene_reconstruction_observed
            ),
            "nav2_costmap_updated_from_perception": (
                nvblox_evidence.nav2_costmap_updated_from_perception
            ),
            "dynamic_obstacle_observed": nvblox_evidence.dynamic_obstacle_observed,
            "perception_artifact_refs": list(
                nvblox_evidence.perception_artifact_refs
            ),
            "nvblox_perception_claim_boundary": nvblox_evidence.claim_boundary,
            "nvblox_perception_supports_obstacle_claim_only_with_trajectory": (
                nvblox_evidence
                .supports_obstacle_aware_claim_when_paired_with_trajectory_evidence
            ),
            "obstacle_challenge_required": obstacle_required,
            "obstacle_avoidance_completion_claimed": (
                obstacle_avoidance_completion_claimed
            ),
            "indoor_delivery_route_completion_claimed": (
                indoor_delivery_route_completion_claimed
            ),
            "dropoff_arrival_claimed": indoor_delivery_route_completion_claimed,
            "patrol_leg_completion_claimed": execution["patrol_leg_completion_claimed"],
            "whole_home_loop_completion_claimed": False,
            "cleaning_completion_claimed": False,
            "payload_delivery_completion_claimed": False,
            "mission_delivery_completion_claimed": False,
            "physical_execution_invoked": bool(
                evidence_payload.get("physical_execution_invoked")
            ),
            "robot_motion_observed": motion["robot_motion_observed"],
            "odom_delta_m": motion["odom_delta_m"],
            "robot_motion_observation_source": motion[
                "robot_motion_observation_source"
            ],
            "telemetry_window_ref": motion.get("telemetry_window_ref"),
            "telemetry_raw_logs_ref": motion.get("telemetry_raw_logs_ref"),
            "obstacle_detected": obstacle["obstacle_detected"],
            "costmap_obstacle_observed": obstacle["costmap_obstacle_observed"],
            "bridge_obstacle_avoidance_observed": obstacle[
                "bridge_obstacle_avoidance_observed"
            ],
            "obstacle_avoidance_observed": obstacle["obstacle_avoidance_observed"],
            "obstacle_trajectory_clearance_observed": obstacle[
                "obstacle_trajectory_clearance_observed"
            ],
            "obstacle_trajectory_intersects_obstacle": obstacle[
                "obstacle_trajectory_intersects_obstacle"
            ],
            "obstacle_intersection_point_count": obstacle[
                "obstacle_intersection_point_count"
            ],
            "obstacle_intersection_segment_count": obstacle[
                "obstacle_intersection_segment_count"
            ],
            "obstacle_min_clearance_m": obstacle["obstacle_min_clearance_m"],
            "obstacle_trajectory_geometry_source": obstacle[
                "obstacle_trajectory_geometry_source"
            ],
            "trajectory_lateral_deviation_observed": obstacle[
                "trajectory_lateral_deviation_observed"
            ],
            "max_lateral_deviation_m": obstacle["max_lateral_deviation_m"],
            "blocking_reasons": list(dict.fromkeys(mission_blocking_reasons)),
            "unproven_claims": list(evidence_payload.get("unproven_claims") or []),
            "progress_counted": False,
        },
    }
    summary_payload = result["summary"]
    episode_review = build_mission_episode_review(
        source_summary=summary_payload,
        source_ref=(
            "turtlebot3_home_mission_execution:"
            f"{proposal.get('proposal_id') or 'unknown'}"
        ),
        vehicle_kind=robot_profile,
    )
    episode_review_ref = mission_episode_review_ref(episode_review)
    result["mission_episode_review"] = episode_review.model_dump(mode="json")
    result["mission_episode_review_ref"] = episode_review_ref
    execution["mission_episode_review_ref"] = episode_review_ref
    summary_payload.update(
        {
            "mission_episode_review_ref": episode_review_ref,
            "mission_episode_review_status": episode_review.status,
            "mission_episode_review_passed": episode_review.passed,
            "mission_episode_review_blocked_buckets": list(
                episode_review.blocked_buckets
            ),
            "mission_episode_review_warning_buckets": list(
                episode_review.warning_buckets
            ),
        }
    )
    return result


__all__ = [
    "TURTLEBOT3_HOME_MISSION_EXECUTION_SCHEMA",
    "TURTLEBOT3_INDOOR_MAP_MODEL_SCHEMA",
    "TURTLEBOT3_HOME_MISSION_PLAN_SCHEMA",
    "TurtleBot3MissionJudgmentPoint",
    "TurtleBot3HomeMissionPlan",
    "approve_turtlebot3_home_mission_plan",
    "build_turtlebot3_home_mission_plan",
    "infer_turtlebot_home_robot_profile",
    "infer_turtlebot3_home_mission_kind",
    "instruction_requests_turtlebot3_home_mission",
    "normalize_turtlebot_nav2_robot_profile",
    "run_turtlebot3_home_mission_dispatch",
]
