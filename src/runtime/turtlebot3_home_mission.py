"""TurtleBot3 home-robot mission wrapper for bounded Nav2 simulator control.

This module keeps the home-robot story at mission level. TurtleBot3 can prove a
bounded Nav2 move in simulation when the external ROS2 bridge reports both Nav2
completion and odom motion. It cannot prove cleaning, payload pickup, payload
dropoff, whole-home coverage, or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
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
    MissionAutonomyProposalClassification,
    approve_mission_autonomy_envelope,
    build_mission_autonomy_envelope,
    build_mission_autonomy_recovery_proposal,
    classify_mission_autonomy_recovery_proposal,
)
from src.runtime.nav2_core_action_feasibility_adapter import (
    evaluate_nav2_recovery_candidates_through_core,
    nav2_recovery_policy,
)
from src.runtime.nav2_turtlebot3_mission_contract_runtime import (
    build_nav2_turtlebot3_runtime_contract,
    evaluate_nav2_turtlebot3_runtime_result,
)
from src.runtime.nvblox_perception_evidence import (
    build_nvblox_perception_evidence_from_env_or_responses,
)
from src.runtime.perception_claim import (
    PerceptionClaim,
    build_perception_claims_from_env_or_responses,
)
from src.runtime.perception_visual_observation import (
    build_visual_observations,
    visual_observation_collision_candidates,
)
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
    Ros2Nav2BridgeCommandClient,
    Ros2Nav2BridgeError,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapterConfig,
    build_blocked_ros2_nav2_hardware_adapter_evidence,
)
from src.runtime.trajectory_clearance_3d import (
    assess_ground_robot_trajectory_clearance_3d,
)
from src.runtime.turtlebot3_log_collector import (
    TurtleBot3LogCollectorError,
    build_turtlebot3_nav2_log_diagnostics,
    collect_turtlebot3_log_bundle_from_env,
    turtlebot3_log_bundle_ref_from_env,
)
from src.runtime.turtlebot3_nav2_execution import (
    dispatch_harness_stop as _dispatch_nav2_harness_stop,
    dispatch_nav2_goal as _dispatch_concrete_nav2_goal,
    obstacle_observation_from_responses as _project_obstacle_observation,
    robot_motion_from_responses as _project_robot_motion,
    sidecar_motion_artifacts as _build_sidecar_motion_artifacts,
)
from src.runtime.turtlebot3_recovery_contracts import (
    build_turtlebot3_recovery_contract_bundle,
    planned_segments_sha256 as _planned_segments_sha256,
    recovery_checkpoint_hash as _recovery_checkpoint_hash,
    recovery_resume_state_hash as _recovery_resume_state_hash,
    validate_turtlebot3_recovery_contract_bundle,
    verify_turtlebot3_recovery_outcome,
)
from src.runtime.turtlebot3_route_transition_authority import (
    build_turtlebot3_route_authority_binding,
    evaluate_turtlebot3_segment_transition_authority,
    validate_turtlebot3_route_authority_binding,
)


TURTLEBOT3_HOME_MISSION_PLAN_SCHEMA = "missionos_turtlebot3_home_mission_plan.v1"
TURTLEBOT3_HOME_MISSION_APPROVAL_SCHEMA = (
    "missionos_turtlebot3_home_mission_approval.v2"
)
TURTLEBOT3_HOME_MISSION_EXECUTION_SCHEMA = (
    "missionos_turtlebot3_home_mission_execution.v1"
)
TURTLEBOT3_INDOOR_MAP_MODEL_SCHEMA = "missionos_turtlebot3_indoor_map_model.v1"
TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION = (
    "missionos_turtlebot3_recovery_shadow_comparison.v1"
)
TURTLEBOT3_RECOVERY_REFLEX_SCHEMA_VERSION = (
    "missionos_turtlebot3_recovery_reflex.v1"
)
TURTLEBOT3_CAMERA_PERCEPTION_PIPELINE_SCHEMA_VERSION = (
    "missionos_turtlebot3_camera_perception_pipeline.v1"
)
TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA = "turtlebot3_recovery_checkpoint.v1"
TURTLEBOT3_RECOVERY_CHECKPOINT_REVISION_SCHEMA = (
    "missionos_turtlebot3_recovery_checkpoint_revision.v1"
)
TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_SCHEMA = (
    "missionos_turtlebot3_recovery_operator_approval.v1"
)
TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION"
)
TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_M"
)
TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_COUNT_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_COUNT"
)
TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_INTERVAL_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_INTERVAL_S"
)
TURTLEBOT3_RECOVERY_PLAN_ONLY_STABILITY_SNAPSHOT_COUNT_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_PLAN_ONLY_STABILITY_SNAPSHOT_COUNT"
)
TURTLEBOT3_SIMULATE_POST_RECOVERY_ROUTE_FAILURE_ONCE_ENV = (
    "MISSIONOS_TURTLEBOT3_SIMULATE_POST_RECOVERY_ROUTE_FAILURE_ONCE"
)

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
_TURTLEBOT3_RECOVERY_REVISION_LONGITUDINAL_BUFFER_M = 0.25
_TURTLEBOT3_RECOVERY_REVISION_WIDE_BBOX_CLEARANCE_M = 0.55
_TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M = 0.15
_TURTLEBOT3_RECOVERY_REVISION_MAX_LATERAL_OFFSET_M = 1.0
_TURTLEBOT3_RECOVERY_REVISION_MAX_DETOUR_DISTANCE_M = 3.5
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
_TURTLEBOT3_STOCK_COLLISION_ENVELOPE = {
    # Conservative union of the collision shapes in the stock waffle_pi SDF,
    # including the rear casters and LiDAR collision. The camera-enabled smoke
    # selects waffle_pi; the envelope also contains the smaller burger body.
    "radius_m": 0.19,
    "z_min_m": -0.01,
    "z_max_m": 0.14,
    "frame_id": "base_footprint",
    "geometry_source": (
        "turtlebot3_waffle_pi_model_sdf_conservative_collision_envelope"
    ),
}
_TURTLEBOT3_HOME_LAYOUT_OBSTACLES = (
    {
        "name": "missionos_closed_door_blocker",
        "kind": "simulated_closed_door",
        "x_m": _TURTLEBOT3_DELIVERY_OBSTACLE_X_M,
        "y_m": _TURTLEBOT3_DELIVERY_OBSTACLE_Y_M,
        "size_x_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_X_M,
        "size_y_m": _TURTLEBOT3_DELIVERY_OBSTACLE_SIZE_Y_M,
        "collision_z_m": 0.25,
        "collision_size_x_m": 0.32,
        "collision_size_y_m": 0.32,
        "collision_size_z_m": 0.5,
        "label": "closed door",
        "label_offset_y_px": -8,
    },
    {
        "name": "missionos_humanoid_blocker",
        "kind": "simulated_humanoid_blocker",
        "x_m": -1.00,
        "y_m": 0.55,
        "size_x_m": 0.24,
        "size_y_m": 0.24,
        "collision_z_m": 0.89,
        "collision_size_x_m": 0.42,
        "collision_size_y_m": 0.62,
        "collision_size_z_m": 1.78,
        "label": "humanoid",
        "label_offset_y_px": 34,
    },
    {
        "name": "missionos_robot_dog_blocker",
        "kind": "simulated_robot_dog_blocker",
        "x_m": 0.70,
        "y_m": 0.55,
        "size_x_m": 0.16,
        "size_y_m": 0.16,
        "collision_z_m": 0.285,
        "collision_size_x_m": 1.04,
        "collision_size_y_m": 0.45,
        "collision_size_z_m": 0.57,
        "label": "robot dog",
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
    # Stop before the obstacle's local-costmap lethal footprint. Recovery
    # candidate evaluation must begin from an executable observation pose,
    # rather than after the robot has already entered the blocked cell.
    x_m=-1.60,
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
        x_m=-1.60,
        y_m=-0.85,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_obstacle_avoidance_waypoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        # Keep the lower-corridor checkpoint centred between the world pillars
        # at (-1.1, -1.1) and (0, -1.1). The old (-0.35, -0.85) target was
        # locally lethal (cost 253), so a source-bound retry could never pass
        # the same controller constraints used by execution.
        x_m=-0.55,
        y_m=-1.55,
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
        # Stay south of the full robot-dog collision volume. Candidate labels
        # do not affect this detour; it is derived from the source-backed AABB.
        x_m=1.50,
        y_m=-0.55,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_living_room_turn_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=1.50,
        y_m=-1.00,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_bookshelf_aisle_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=1.10,
        y_m=-1.50,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="simulated_alternate_door_checkpoint",
    ),
    Nav2GoalPose(
        frame_id="map",
        x_m=0.20,
        y_m=-1.50,
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
TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL"
)
TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV = (
    "MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED"
)
TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV = (
    "MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON"
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
# Keep the default living-room dropoff outside the overlapping inflated-cost
# regions around the table, simulated person, and simulated pet.  The former
# (-1.4, 2.42) target was geometrically open at the robot centre but left the
# local controller boxed into three nearby inflated obstacles.  This point is
# just inside the living-room entrance and preserves at least ~1.07 m static
# clearance in the checked-in house floor plan, plus >2 m to dynamic blockers.
_TURTLEBOT3_HOUSE_LIVING_DROPOFF_X_M = -4.0
_TURTLEBOT3_HOUSE_LIVING_DROPOFF_Y_M = 1.15
_TURTLEBOT3_HOUSE_ROUTE_SEGMENTS = (
    # Stay south of the closed-door challenge volume while crossing the open
    # front yard.  The former -0.8 -> -0.9 leg left only ~1 cm between the
    # 0.19 m robot envelope and the box; the north-side alternative approached
    # the house wall and could stall Nav2 before the mailbox.  The south leg is
    # source-backed by observed free-space traversal in the house smoke.
    _house_goal_pose(-0.5, -1.75, "simulated_front_yard_checkpoint"),
    _house_goal_pose(1.15, -1.75, "simulated_mailbox_approach_checkpoint"),
    _house_goal_pose(1.11, 0.45, "simulated_front_door_passage_checkpoint"),
    _house_goal_pose(-0.9, 0.4, "simulated_hallway_checkpoint"),
    # Stay south-west of the full humanoid collision volume before turning
    # through the living-room entrance.
    _house_goal_pose(-2.5, 0.7, "simulated_living_room_south_detour_checkpoint"),
    _house_goal_pose(-2.66, 1.5, "simulated_living_room_entry_checkpoint"),
    _house_goal_pose(
        _TURTLEBOT3_HOUSE_LIVING_DROPOFF_X_M,
        _TURTLEBOT3_HOUSE_LIVING_DROPOFF_Y_M,
        "simulated_table_dropoff_waypoint",
    ),
)
_TURTLEBOT3_HOUSE_ROUTE_PREFIX = _TURTLEBOT3_HOUSE_ROUTE_SEGMENTS[:5]
# Destination registry: branch waypoints continue from the hallway checkpoint
# and pass only through real door openings extracted from the house SDF.
_TURTLEBOT3_HOUSE_ROOM_DESTINATIONS = {
    "living": {
        "label": "Living room",
        "terms": ("living", "リビング", "居間"),
        "via": ((-2.66, 1.5, "simulated_living_room_entry_checkpoint"),),
        "dropoff": (
            _TURTLEBOT3_HOUSE_LIVING_DROPOFF_X_M,
            _TURTLEBOT3_HOUSE_LIVING_DROPOFF_Y_M,
            "simulated_living_room_dropoff_waypoint",
        ),
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
        "collision_z_m": 0.25,
        "collision_size_x_m": 0.32,
        "collision_size_y_m": 0.32,
        "collision_size_z_m": 0.5,
        "label": "closed door",
        "label_offset_y_px": -8,
    },
    {
        "name": "missionos_humanoid_blocker",
        "kind": "simulated_humanoid_blocker",
        "x_m": -1.75,
        "y_m": 1.6,
        "size_x_m": 0.24,
        "size_y_m": 0.24,
        "collision_z_m": 0.89,
        "collision_size_x_m": 0.42,
        "collision_size_y_m": 0.62,
        "collision_size_z_m": 1.78,
        "label": "humanoid",
        "label_offset_y_px": 34,
    },
    {
        "name": "missionos_robot_dog_blocker",
        "kind": "simulated_robot_dog_blocker",
        "x_m": -0.7,
        "y_m": 2.6,
        "size_x_m": 0.16,
        "size_y_m": 0.16,
        "collision_z_m": 0.285,
        "collision_size_x_m": 1.04,
        "collision_size_y_m": 0.45,
        "collision_size_z_m": 0.57,
        "label": "robot dog",
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


def _recovery_requires_fresh_approval() -> bool:
    return _truthy_env(TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV)


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
        "検出したら",
        "検知したら",
        "見つけたら",
        "出現",
        "現れ",
        "mid-mission",
        "during",
        "appears",
        "encounter",
        "detects",
        "detected",
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
        "reflex_first_recovery_entry": True,
        "requires_recorded_skip_reason": True,
        "claim_boundary": (
            "The emergency harness may stop motion, but it must record why "
            "LLM recovery proposal generation was skipped."
        ),
    }


def _load_applied_recovery_promotions() -> tuple[Any, ...]:
    """Load operator-applied recovery-action promotions, fail-closed.

    The env points at the applications file written by the promotion CLI
    (scripts/turtlebot3_recovery_promotion_cli.py). Every entry must
    validate as a full RecoveryActionPromotionApplication — which requires a
    non-empty operator_approval_ref — or it is ignored. An unreadable or
    malformed file widens nothing.
    """

    from src.runtime.recovery_action_promotion import (
        RecoveryActionPromotionApplication,
    )

    path_value = os.environ.get(
        TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, ""
    ).strip()
    if not path_value:
        return ()
    try:
        raw = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    applications = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            applications.append(
                RecoveryActionPromotionApplication.model_validate(dict(entry))
            )
        except Exception:
            continue
    return tuple(applications)


def _build_autonomy_envelope(
    *,
    proposal_id: str,
    operator_approved: bool = False,
    operator_approval_ref: str | None = None,
) -> dict[str, Any]:
    preapproved_recovery_actions = ("return_home", "hold", "avoid_obstacle")
    requires_human_approval_for = ("reroute", "safe_stop", "ask_human")
    applied_promotion_refs: tuple[str, ...] = ()
    if _recovery_requires_fresh_approval():
        preapproved_recovery_actions = ()
        requires_human_approval_for = (
            "return_home",
            "hold",
            "avoid_obstacle",
            "reroute",
            "safe_stop",
            "ask_human",
        )
    elif _recovery_avoid_obstacle_requires_fresh_approval():
        preapproved_recovery_actions = ("return_home", "hold")
        requires_human_approval_for = (
            "avoid_obstacle",
            "reroute",
            "safe_stop",
            "ask_human",
        )
    if not _recovery_requires_fresh_approval():
        # Precedence: the master tighten env above is a safety switch and
        # blocks all promotions. Below it, an operator-applied promotion
        # (a later, evidence-backed, recorded decision) may return an
        # action to preapproved — including one the avoid_obstacle tighten
        # env demoted, which is exactly the demote -> accumulate evidence
        # -> promote-back workflow.
        promotable = {"avoid_obstacle", "reroute", "safe_stop", "ask_human"}
        for application in _load_applied_recovery_promotions():
            if (
                application.action in promotable
                and application.action in requires_human_approval_for
            ):
                preapproved_recovery_actions = (
                    *preapproved_recovery_actions,
                    application.action,
                )
                requires_human_approval_for = tuple(
                    action
                    for action in requires_human_approval_for
                    if action != application.action
                )
                applied_promotion_refs = (
                    *applied_promotion_refs,
                    application.application_id,
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
        applied_recovery_promotions=applied_promotion_refs,
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


def _recovery_reflex_record(
    *,
    trigger: str,
    runtime_motion_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the reflex phase entered before LLM deliberation.

    Two-phase recovery: the deterministic reflex layer assesses the safe
    posture first, then deliberation runs against a stabilized robot. This
    record documents the assessment; stop dispatch stays with the emergency
    harness and is never performed by the record itself.
    """

    if not runtime_motion_context:
        motion_state = "pre_dispatch"
    elif runtime_motion_context.get("stalled_after_dispatch") is True:
        motion_state = "stationary"
    elif runtime_motion_context.get("robot_motion_observed") is True:
        motion_state = "moving"
    elif runtime_motion_context.get("robot_motion_observed") is False:
        motion_state = "stationary"
    else:
        motion_state = "unknown"
    return {
        "schema_version": TURTLEBOT3_RECOVERY_REFLEX_SCHEMA_VERSION,
        "trigger": trigger,
        "reflex_action": "hold",
        "robot_motion_state": motion_state,
        "motion_observation_source": str(
            runtime_motion_context.get("motion_observation_source") or ""
        ),
        "stop_dispatch_required": motion_state == "moving",
        "stop_dispatch_performed": False,
        "entered_deliberation": True,
        "claim_boundary": (
            "The reflex record assesses the safe posture before deliberation; "
            "any actual stop is dispatched by the emergency harness and must "
            "record its reason. robot_motion_state derives from the last "
            "segment's bridge receipt, not a live reading — 'moving' means "
            "motion cannot be ruled out, so the stop errs toward dispatching "
            "a redundant cancel."
        ),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _recovery_shadow_comparison(
    *,
    deterministic_candidate: MissionAutonomyRecoveryProposal,
    deterministic_trigger: str,
    planner_result: Mapping[str, Any],
    llm_proposal: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Record LLM-vs-deterministic disagreement; measurement only, no authority."""

    llm_action = str((llm_proposal or {}).get("selected_action") or "")
    llm_proposal_available = bool(llm_action)
    return {
        "schema_version": TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION,
        "deterministic_action": deterministic_candidate.selected_action,
        "deterministic_trigger": deterministic_trigger,
        "llm_action": llm_action,
        "llm_proposal_available": llm_proposal_available,
        "planner_status": str(planner_result.get("planner_status") or ""),
        "agreement": (
            llm_action == deterministic_candidate.selected_action
            if llm_proposal_available
            else None
        ),
        "measurement_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


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
    harness_stop_dispatcher: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None,
    perception_claims: Sequence[PerceptionClaim] = (),
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
    if battery_recovery_required:
        deterministic_trigger = "battery_envelope_below_reserve"
        deterministic_candidate = _deterministic_return_home_recovery_proposal(
            proposal_id=proposal_id,
            battery_envelope=battery_envelope,
            home_distance_envelope=home_distance_envelope,
        )
    elif failure_recovery_required:
        deterministic_trigger = "runtime_segment_failure"
        deterministic_candidate = (
            _deterministic_return_home_after_failure_recovery_proposal(
                proposal_id=proposal_id,
                runtime_failure_context=failure_context,
                runtime_motion_context=motion_context,
                home_distance_envelope=home_distance_envelope,
            )
        )
    else:
        deterministic_trigger = "runtime_obstacle_observed"
        deterministic_candidate = _deterministic_avoid_obstacle_recovery_proposal(
            proposal_id=proposal_id,
            obstacle_scenario=obstacle_scenario,
        )
    recovery_reflex = _recovery_reflex_record(
        trigger=deterministic_trigger,
        runtime_motion_context=motion_context,
    )
    harness_stop_dispatch: dict[str, Any] = {}
    if (
        recovery_reflex["stop_dispatch_required"]
        and harness_stop_dispatcher is not None
    ):
        harness_stop_dispatch = dict(harness_stop_dispatcher(recovery_reflex))
        recovery_reflex["stop_dispatch_performed"] = (
            harness_stop_dispatch.get("cancel_accepted") is True
        )
        recovery_reflex["stop_confirmed"] = (
            harness_stop_dispatch.get("stop_confirmed") is True
        )
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
        perception_claims=perception_claims,
    )
    planner_result = {
        **dict(planner_result),
        "gemini_credential_status": os.environ.get(
            "MISSIONOS_GEMINI_CREDENTIAL_STATUS",
            "not_reported",
        ),
        "recovery_reflex": recovery_reflex,
        "harness_stop_dispatch": harness_stop_dispatch,
        "perception_claims": [
            claim.model_dump(mode="json") for claim in perception_claims
        ],
    }
    planner_proposal = planner_result.get("proposal")
    guardrail_passed = (
        planner_result.get("planner_status") == "proposal_guardrail_passed"
        and isinstance(planner_proposal, Mapping)
        and bool(planner_proposal)
    )
    planner_result["shadow_comparison"] = _recovery_shadow_comparison(
        deterministic_candidate=deterministic_candidate,
        deterministic_trigger=deterministic_trigger,
        planner_result=planner_result,
        llm_proposal=dict(planner_proposal) if guardrail_passed else None,
    )
    if guardrail_passed:
        accepted = MissionAutonomyRecoveryProposal.model_validate(
            dict(planner_proposal)
        )
        return (accepted,), {
            **dict(planner_result),
            "proposal_source": accepted.proposal_source,
            "deterministic_fallback_used": False,
        }
    return (deterministic_candidate,), {
        **dict(planner_result),
        "proposal_source": "deterministic_fallback",
        "deterministic_fallback_used": True,
        "fallback_reason": (
            "gemini_credentials_missing"
            if planner_result.get("gemini_credential_status")
            == "missing_deterministic_fallback"
            else "planner_proposal_unavailable_or_guardrail_rejected"
        ),
    }


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
        "route_layout": "simulated_home_loop_with_closed_door_humanoid_and_robot_dog_detours"
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


def _battery_return_decision(
    *,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    battery_pct = battery_envelope.get("battery_start_pct")
    return_energy_pct = home_distance_envelope.get(
        "projected_return_battery_required_pct"
    )
    reserve_pct = home_distance_envelope.get("projected_return_reserve_pct")
    if not isinstance(battery_pct, (int, float)) or isinstance(battery_pct, bool):
        return {}
    if not isinstance(return_energy_pct, (int, float)) or isinstance(
        return_energy_pct,
        bool,
    ):
        return {}
    if not isinstance(reserve_pct, (int, float)) or isinstance(reserve_pct, bool):
        reserve_pct = 5.0
    approval_and_replan_margin_pct = 3.0
    return_trigger_pct = (
        float(return_energy_pct)
        + float(reserve_pct)
        + approval_and_replan_margin_pct
    )
    margin_to_return_pct = float(battery_pct) - (
        float(return_energy_pct) + float(reserve_pct)
    )
    critical_hold_pct = 10.0
    policy_state = (
        "critical_hold"
        if float(battery_pct) <= critical_hold_pct
        else "awaiting_return_approval"
        if float(battery_pct) <= return_trigger_pct
        else "normal"
    )
    return {
        "schema_version": "missionos_turtlebot3_battery_return_decision.v1",
        "battery_pct_observed": float(battery_pct),
        "predicted_return_energy_pct": float(return_energy_pct),
        "reserve_pct": float(reserve_pct),
        "approval_and_replan_margin_pct": approval_and_replan_margin_pct,
        "return_trigger_pct": round(return_trigger_pct, 3),
        "margin_to_return_pct": round(margin_to_return_pct, 3),
        "battery_policy_state": policy_state,
        "return_home_proposal_required": policy_state
        == "awaiting_return_approval",
        "emergency_hold_required": policy_state == "critical_hold",
        "continuous_monitoring_status": "fixture_snapshot_only",
        "dispatch_authority_created": False,
        "automatic_return_home_dispatched": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "claim_boundary": (
            "This snapshot may trigger a return-home proposal or critical hold. "
            "It does not approve or dispatch return_home."
        ),
    }


def _runtime_recovery_obstacle_scenario(
    obstacle_scenario: Mapping[str, Any],
    *,
    segment_result: Mapping[str, Any],
) -> dict[str, Any]:
    obstacle_x_m, obstacle_y_m = _profile_delivery_obstacle_xy()
    scene_marker = next(
        (
            marker
            for marker in _profile_layout_obstacles()
            if math.isclose(float(marker.get("x_m") or 0.0), obstacle_x_m)
            and math.isclose(float(marker.get("y_m") or 0.0), obstacle_y_m)
        ),
        {},
    )
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
        "runtime_obstacle_x_m": obstacle_x_m,
        "runtime_obstacle_y_m": obstacle_y_m,
        "runtime_obstacle_size_x_m": scene_marker.get("size_x_m"),
        "runtime_obstacle_size_y_m": scene_marker.get("size_y_m"),
        "runtime_obstacle_z_m": scene_marker.get("collision_z_m"),
        "runtime_obstacle_collision_size_x_m": scene_marker.get(
            "collision_size_x_m"
        ),
        "runtime_obstacle_collision_size_y_m": scene_marker.get(
            "collision_size_y_m"
        ),
        "runtime_obstacle_size_z_m": scene_marker.get("collision_size_z_m"),
        "runtime_obstacle_frame_id": "map",
        "runtime_obstacle_scene_ref": scene_marker.get("name"),
        "runtime_obstacle_geometry_source": (
            "opt_in_turtlebot3_home_loop_obstacle_smoke_scene"
        ),
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


def _recovery_candidate_clearance_m() -> float:
    raw = os.environ.get(TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.75
    else:
        value = 0.75
    return min(max(value, 0.55), 1.5)


def _deterministic_recovery_candidates(
    obstacle_scenario: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Generate bounded candidates; live Nav2 evidence decides validity."""

    obstacle_x = float(obstacle_scenario.get("runtime_obstacle_x_m") or 0.0)
    obstacle_y = float(obstacle_scenario.get("runtime_obstacle_y_m") or 0.0)
    size_x = float(obstacle_scenario.get("runtime_obstacle_size_x_m") or 0.32)
    size_y = float(obstacle_scenario.get("runtime_obstacle_size_y_m") or 0.32)
    clearance = _recovery_candidate_clearance_m()
    x_offset = size_x / 2.0 + clearance
    y_offset = size_y / 2.0 + clearance
    max_speed_mps = _profile_dynamic_obstacle_avoidance_goal().max_speed_mps
    return [
        {
            "candidate_id": "obstacle_bypass_south",
            "side": "right",
            "selection_role": "route_lateral_bypass",
            "selection_priority": 0,
            "x_m": obstacle_x,
            "y_m": obstacle_y - y_offset,
            "yaw_rad": 0.0,
            "max_speed_mps": max_speed_mps,
            "geometry_clearance_m": clearance,
            "geometry_source": "obstacle_bbox_plus_clearance",
        },
        {
            "candidate_id": "obstacle_bypass_north",
            "side": "left",
            "selection_role": "route_lateral_bypass",
            "selection_priority": 0,
            "x_m": obstacle_x,
            "y_m": obstacle_y + y_offset,
            "yaw_rad": 0.0,
            "max_speed_mps": max_speed_mps,
            "geometry_clearance_m": clearance,
            "geometry_source": "obstacle_bbox_plus_clearance",
        },
        {
            "candidate_id": "obstacle_bypass_west",
            "side": "backtrack",
            "selection_role": "retreat_fallback",
            "selection_priority": 2,
            "x_m": obstacle_x - x_offset,
            "y_m": obstacle_y,
            "yaw_rad": 0.0,
            "max_speed_mps": max_speed_mps,
            "geometry_clearance_m": clearance,
            "geometry_source": "obstacle_bbox_plus_clearance",
        },
        {
            "candidate_id": "obstacle_bypass_east",
            "side": "forward",
            "selection_role": "forward_fallback",
            "selection_priority": 1,
            "x_m": obstacle_x + x_offset,
            "y_m": obstacle_y,
            "yaw_rad": 0.0,
            "max_speed_mps": max_speed_mps,
            "geometry_clearance_m": clearance,
            "geometry_source": "obstacle_bbox_plus_clearance",
        },
    ]


def _observed_inbound_retreat_candidate(
    segment_results: list[dict[str, Any]],
    *,
    retreat_distance_m: float = 0.45,
) -> dict[str, Any] | None:
    """Pick a bounded retreat point from bridge-observed map-frame motion."""

    if not segment_results:
        return None
    samples: list[dict[str, Any]] = []
    for response in segment_results[-1].get("bridge_responses") or []:
        if isinstance(response, Mapping):
            samples.extend(_trajectory_samples_from_response(response))
    map_samples = [
        sample
        for sample in samples
        if str(sample.get("frame_id") or "").lower() == "map"
        and sample.get("observed_trajectory_evidence_eligible") is True
    ]
    if len(map_samples) < 2:
        return None
    current = map_samples[-1]
    current_x = float(current["x_m"])
    current_y = float(current["y_m"])
    prior_x = current_x
    prior_y = current_y
    traversed_m = 0.0
    target_xy: tuple[float, float] | None = None
    for sample in reversed(map_samples[:-1]):
        sample_x = float(sample["x_m"])
        sample_y = float(sample["y_m"])
        segment_m = math.hypot(prior_x - sample_x, prior_y - sample_y)
        if segment_m <= 1e-9:
            continue
        if traversed_m + segment_m >= retreat_distance_m:
            remaining_m = retreat_distance_m - traversed_m
            fraction = min(1.0, remaining_m / segment_m)
            target_xy = (
                prior_x + fraction * (sample_x - prior_x),
                prior_y + fraction * (sample_y - prior_y),
            )
            break
        traversed_m += segment_m
        prior_x = sample_x
        prior_y = sample_y
    if target_xy is None:
        return None
    target_x, target_y = target_xy
    yaw = math.atan2(
        current_y - target_y,
        current_x - target_x,
    )
    return {
        "candidate_id": "observed_inbound_bounded_retreat",
        "side": "backtrack",
        "selection_role": "verified_inbound_retreat",
        "selection_priority": -1,
        "sequence_only": True,
        "x_m": target_x,
        "y_m": target_y,
        "yaw_rad": yaw,
        "max_speed_mps": _profile_dynamic_obstacle_avoidance_goal().max_speed_mps,
        "retreat_distance_bound_m": retreat_distance_m,
        "geometry_source": "bridge_observed_inbound_map_trajectory",
    }


def _recovery_sequence_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind every downstream path check to the prior recovery goal pose."""

    chained: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(candidates):
        candidate = dict(raw_candidate)
        if index > 0:
            previous = chained[index - 1]
            candidate["start_pose"] = {
                "x_m": float(previous["x_m"]),
                "y_m": float(previous["y_m"]),
                "yaw_rad": float(previous.get("yaw_rad") or 0.0),
            }
        chained.append(candidate)
    return chained


def _plan_only_recovery_evaluation_retryable(
    evaluation: Mapping[str, Any],
) -> bool:
    if evaluation.get("evaluation_status") == "validated":
        return False
    transient_reasons = {
        "nav2_compute_path_not_succeeded",
        "nav2_compute_path_empty",
        "recovery_path_cost_unavailable",
        "recovery_current_pose_outside_local_costmap",
        "recovery_local_path_cost_unavailable",
        "recovery_local_path_prefix_insufficient",
    }
    return any(
        transient_reasons.intersection(item.get("blocking_reasons") or ())
        for item in evaluation.get("candidate_evaluations") or ()
        if isinstance(item, Mapping)
    )


def _evaluate_recovery_candidates_plan_only(
    *,
    candidates: list[dict[str, Any]],
    obstacle: Mapping[str, Any],
    frame_id: str = "map",
    previous_hazard_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh transient Nav2 snapshots without creating dispatch authority."""

    try:
        retry_count = max(
            0,
            min(
                4,
                int(
                    os.environ.get(
                        TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_COUNT_ENV,
                        "2",
                    )
                ),
            ),
        )
    except ValueError:
        retry_count = 2
    try:
        retry_interval_s = max(
            0.0,
            min(
                5.0,
                float(
                    os.environ.get(
                        TURTLEBOT3_RECOVERY_PLAN_ONLY_RETRY_INTERVAL_ENV,
                        "1.0",
                    )
                ),
            ),
        )
    except ValueError:
        retry_interval_s = 1.0
    try:
        stability_snapshot_count = max(
            1,
            min(
                retry_count + 1,
                int(
                    os.environ.get(
                        TURTLEBOT3_RECOVERY_PLAN_ONLY_STABILITY_SNAPSHOT_COUNT_ENV,
                        "1",
                    )
                ),
            ),
        )
    except ValueError:
        stability_snapshot_count = 1
    evaluation: dict[str, Any] = {}
    last_error: Ros2Nav2BridgeError | None = None
    for attempt_index in range(retry_count + 1):
        try:
            evaluation = (
                Ros2Nav2BridgeCommandClient().evaluate_recovery_candidates(
                    candidates=candidates,
                    obstacle=obstacle,
                    frame_id=frame_id,
                )
            )
            last_error = None
        except Ros2Nav2BridgeError as exc:
            last_error = exc
        should_retry = (
            attempt_index < retry_count
            and (
                attempt_index + 1 < stability_snapshot_count
                or
                last_error is not None
                or _plan_only_recovery_evaluation_retryable(evaluation)
            )
        )
        if not should_retry:
            break
        if retry_interval_s:
            time.sleep(retry_interval_s)
    if last_error is not None:
        raise last_error
    observed_at = str(
        evaluation.get("observation_captured_at")
        or datetime.now(timezone.utc).isoformat()
    )
    raw_evaluation = {
        **evaluation,
        "plan_only_evaluation_attempt_count": attempt_index + 1,
        "plan_only_retry_performed": attempt_index > 0,
        "plan_only_stability_snapshot_count_required": (
            stability_snapshot_count
        ),
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    return evaluate_nav2_recovery_candidates_through_core(
        evaluation=raw_evaluation,
        obstacle=obstacle,
        robot_collision_envelope=_TURTLEBOT3_STOCK_COLLISION_ENVELOPE,
        active_policy=nav2_recovery_policy(),
        evaluated_at=observed_at,
        previous_hazard_state=previous_hazard_state,
    )


def _resolve_recovery_candidate(
    obstacle_scenario: Mapping[str, Any],
    *,
    segment_results: list[dict[str, Any]] | None = None,
    excluded_candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    candidates = _deterministic_recovery_candidates(obstacle_scenario)
    excluded = set(excluded_candidate_ids or ())
    candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("candidate_id") or "") not in excluded
    ]
    retreat = _observed_inbound_retreat_candidate(segment_results or [])
    if retreat is not None:
        candidates.append(retreat)
    base = {
        "schema_version": "missionos_nav2_recovery_candidate_resolution.v1",
        "candidate_generation": "deterministic_obstacle_bbox_clearance.v1",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "command_ack_observed": False,
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    if not _truthy_env(TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV):
        selected = next(
            (item for item in candidates if item.get("sequence_only") is not True),
            candidates[0],
        )
        return {
            **base,
            "resolution_status": "fixture_geometry_only",
            "selected_candidate": selected,
            "live_costmap_validated": False,
            "blocking_reasons": [],
            "claim_boundary": (
                "Geometry-only selection is for deterministic fixtures. Live "
                "sim dispatch requires plan-only Nav2 validation."
            ),
        }
    try:
        evaluation = _evaluate_recovery_candidates_plan_only(
            candidates=candidates,
            obstacle=obstacle_scenario,
            frame_id="map",
        )
    except Ros2Nav2BridgeError as exc:
        return {
            **base,
            "resolution_status": "blocked",
            "selected_candidate": None,
            "live_costmap_validated": False,
            "blocking_reasons": [
                f"nav2_recovery_candidate_evaluation_failed:{type(exc).__name__}"
            ],
            "error": str(exc)[:400],
        }
    selected = evaluation.get("selected_candidate")
    selected = dict(selected) if isinstance(selected, Mapping) else None
    validated = (
        evaluation.get("evaluation_status") == "validated"
        and selected is not None
        and selected.get("path_valid") is True
        and selected.get("core_action_feasibility_status")
        == "verified_feasible"
    )
    evaluations = [
        dict(item)
        for item in evaluation.get("candidate_evaluations") or []
        if isinstance(item, Mapping)
    ]
    retreat_evaluation = next(
        (
            item
            for item in evaluations
            if item.get("candidate_id") == "observed_inbound_bounded_retreat"
            and item.get("path_valid") is True
            and item.get("core_action_feasibility_status")
            == "verified_feasible"
        ),
        None,
    )
    initial_evaluations = list(evaluations)
    # A bypass that was validated directly from the current robot pose needs
    # no preliminary retreat. The retreat is a fallback only when direct
    # candidates are invalid; forcing it here adds an unnecessary second goal
    # whose cost can change before the final sequence recheck.
    selected_sequence = [dict(selected)] if validated else []
    # If every direct bypass starts inside the current local inflation cost,
    # the bridge correctly rejects those paths while still validating the
    # short observed inbound retreat. Evaluate each bypass again from the end
    # of that retreat. Each pair is independent; chaining all bypasses in one
    # request would incorrectly make candidate N start at candidate N-1.
    if not validated and retreat_evaluation is not None:
        retreat_candidate = next(
            (
                dict(item)
                for item in candidates
                if item.get("candidate_id")
                == "observed_inbound_bounded_retreat"
            ),
            None,
        )
        sequence_attempts: list[
            tuple[
                dict[str, Any],
                list[dict[str, Any]],
                list[dict[str, Any]],
            ]
        ] = []
        if retreat_candidate is not None:
            for bypass_candidate in candidates:
                if bypass_candidate.get("sequence_only") is True:
                    continue
                sequence_candidates = _recovery_sequence_candidates(
                    [dict(retreat_candidate), dict(bypass_candidate)]
                )
                try:
                    sequence_evaluation = (
                        _evaluate_recovery_candidates_plan_only(
                            candidates=sequence_candidates,
                            obstacle=obstacle_scenario,
                            frame_id="map",
                        )
                    )
                except Ros2Nav2BridgeError:
                    continue
                sequence_evaluations = [
                    dict(item)
                    for item in sequence_evaluation.get("candidate_evaluations")
                    or []
                    if isinstance(item, Mapping)
                ]
                sequence_by_id = {
                    str(item.get("candidate_id") or ""): item
                    for item in sequence_evaluations
                }
                evaluated_sequence = [
                    dict(
                        sequence_by_id.get(
                            str(candidate.get("candidate_id") or "")
                        )
                        or {}
                    )
                    for candidate in sequence_candidates
                ]
                if (
                    sequence_evaluation.get("evaluation_status") == "validated"
                    and all(
                        candidate.get("path_valid") is True
                        and candidate.get("core_action_feasibility_status")
                        == "verified_feasible"
                        for candidate in evaluated_sequence
                    )
                ):
                    sequence_attempts.append(
                        (
                            dict(sequence_evaluation),
                            sequence_evaluations,
                            evaluated_sequence,
                        )
                    )
        if sequence_attempts:
            sequence_attempts.sort(
                key=lambda attempt: (
                    int(
                        attempt[2][-1].get("local_maximum_path_cost") or 0
                    ),
                    int(attempt[2][-1].get("selection_priority", 100)),
                    int(attempt[2][-1].get("maximum_path_cost") or 0),
                    float(attempt[2][-1].get("path_length_m") or math.inf),
                    str(attempt[2][-1].get("candidate_id") or ""),
                )
            )
            evaluation, evaluations, selected_sequence = sequence_attempts[0]
            selected = dict(selected_sequence[-1])
            validated = True
    if len(selected_sequence) > 1:
        sequence_candidates = _recovery_sequence_candidates(selected_sequence)
        try:
            sequence_evaluation = (
                _evaluate_recovery_candidates_plan_only(
                    candidates=sequence_candidates,
                    obstacle=obstacle_scenario,
                    frame_id="map",
                )
            )
        except Ros2Nav2BridgeError as exc:
            return {
                **base,
                "resolution_status": "blocked",
                "selected_candidate": None,
                "live_costmap_validated": False,
                "bounded_retreat_required": True,
                "initial_candidate_evaluations": initial_evaluations,
                "blocking_reasons": [
                    "nav2_recovery_sequence_evaluation_failed:"
                    f"{type(exc).__name__}"
                ],
                "error": str(exc)[:400],
            }
        sequence_evaluations = [
            dict(item)
            for item in sequence_evaluation.get("candidate_evaluations") or []
            if isinstance(item, Mapping)
        ]
        sequence_by_id = {
            str(item.get("candidate_id") or ""): item
            for item in sequence_evaluations
        }
        selected_sequence = [
            dict(sequence_by_id.get(str(candidate.get("candidate_id") or "")) or {})
            for candidate in sequence_candidates
        ]
        validated = (
            sequence_evaluation.get("evaluation_status") == "validated"
            and all(
                candidate.get("path_valid") is True
                and candidate.get("core_action_feasibility_status")
                == "verified_feasible"
                for candidate in selected_sequence
            )
        )
        evaluation = sequence_evaluation
        evaluations = sequence_evaluations
        selected = selected_sequence[-1] if validated else None
        if not validated:
            selected_sequence = []
    return {
        **base,
        "resolution_status": "validated" if validated else "blocked",
        "selected_candidate": selected if validated else None,
        "live_costmap_validated": validated,
        "dual_costmap_validated": validated
        and bool(evaluation.get("global_costmap_snapshot_hash"))
        and bool(evaluation.get("local_costmap_snapshot_hash")),
        "selected_sequence": selected_sequence,
        "bounded_retreat_required": len(selected_sequence) > 1,
        "costmap_snapshot_hash": evaluation.get("costmap_snapshot_hash"),
        "costmap_source": evaluation.get("costmap_source"),
        "global_costmap_snapshot_hash": evaluation.get(
            "global_costmap_snapshot_hash"
        ),
        "global_costmap_source": evaluation.get("global_costmap_source"),
        "local_costmap_snapshot_hash": evaluation.get(
            "local_costmap_snapshot_hash"
        ),
        "local_costmap_source": evaluation.get("local_costmap_source"),
        "local_costmap_frame_id": evaluation.get("local_costmap_frame_id"),
        "core_adapter_id": evaluation.get("core_adapter_id"),
        "core_hazard_state": evaluation.get("core_hazard_state"),
        "core_hazard_state_sha256": evaluation.get(
            "core_hazard_state_sha256"
        ),
        "core_policy_binding": evaluation.get("core_policy_binding"),
        "local_cost_threshold": evaluation.get("local_cost_threshold"),
        "compute_path_action": evaluation.get("compute_path_action"),
        "plan_only_evaluation_attempt_count": evaluation.get(
            "plan_only_evaluation_attempt_count",
            1,
        ),
        "plan_only_retry_performed": evaluation.get(
            "plan_only_retry_performed",
            False,
        ),
        "candidate_evaluations": evaluations,
        "initial_candidate_evaluations": initial_evaluations,
        "blocking_reasons": list(evaluation.get("blocking_reasons") or []),
        "claim_boundary": (
            "Plan-only Nav2 evaluation creates no dispatch authority. The selected "
            "candidate still requires a fresh checkpoint-bound approval."
        ),
    }


def _revalidate_approved_recovery_candidate(
    *,
    checkpoint: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate the exact approved target immediately before dispatch."""

    if checkpoint.get("selected_action") not in {"avoid_obstacle", "reroute"}:
        return {
            "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
            "revalidation_status": "not_required",
            "dispatch_request_sent": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    if not _truthy_env(TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV):
        return {
            "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
            "revalidation_status": "fixture_not_requested",
            "dispatch_request_sent": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    parameters = checkpoint.get("approved_parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    binding = checkpoint.get("recovery_candidate_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    if (
        binding.get("live_costmap_validated") is not True
        or binding.get("dual_costmap_validated") is not True
        or (
            binding.get("core_action_feasibility_required") is True
            and not isinstance(binding.get("core_hazard_state"), Mapping)
        )
    ):
        return {
            "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
            "revalidation_status": "blocked",
            "blocking_reasons": [
                "checkpoint_recovery_candidate_not_fully_verified"
            ],
            "dispatch_request_sent": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    raw_goals = checkpoint.get("recovery_goal_poses")
    raw_goals = raw_goals if isinstance(raw_goals, list) else []
    bound_ids = binding.get("candidate_ids")
    bound_ids = bound_ids if isinstance(bound_ids, list) else []
    try:
        if raw_goals:
            approved_candidates: list[dict[str, Any]] = []
            for index, goal in enumerate(raw_goals):
                if not isinstance(goal, Mapping):
                    continue
                candidate_id = str(
                    bound_ids[index]
                    if index < len(bound_ids)
                    else f"approved_recovery_target_{index + 1}"
                )
                candidate = {
                    "candidate_id": candidate_id,
                    "x_m": float(goal["x_m"]),
                    "y_m": float(goal["y_m"]),
                    "yaw_rad": float(goal.get("yaw_rad") or 0.0),
                    "max_speed_mps": float(
                        goal.get("max_speed_mps")
                        or _profile_dynamic_obstacle_avoidance_goal().max_speed_mps
                    ),
                    "geometry_source": "checkpoint_approved_goal_sequence",
                }
                if candidate_id == "observed_inbound_bounded_retreat":
                    candidate.update(
                        {
                            "selection_role": "verified_inbound_retreat",
                            "sequence_only": True,
                            "retreat_distance_bound_m": 0.45,
                        }
                    )
                approved_candidates.append(candidate)
            candidates = _recovery_sequence_candidates(approved_candidates)
        else:
            candidates = [
                {
                    "candidate_id": str(
                        binding.get("candidate_id") or "approved_recovery_target"
                    ),
                    "x_m": float(parameters["target_x_m"]),
                    "y_m": float(parameters["target_y_m"]),
                    "yaw_rad": float(parameters.get("target_yaw_rad") or 0.0),
                    "max_speed_mps": (
                        _profile_dynamic_obstacle_avoidance_goal().max_speed_mps
                    ),
                    "geometry_source": "checkpoint_approved_parameters",
                }
            ]
    except (KeyError, TypeError, ValueError):
        return {
            "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
            "revalidation_status": "blocked",
            "blocking_reasons": ["approved_recovery_candidate_invalid"],
            "dispatch_request_sent": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    try:
        evaluation = _evaluate_recovery_candidates_plan_only(
            candidates=candidates,
            obstacle=obstacle_scenario,
            frame_id="map",
            previous_hazard_state=(
                dict(binding["core_hazard_state"])
                if isinstance(
                    binding.get("core_hazard_state"),
                    Mapping,
                )
                else None
            ),
        )
    except Ros2Nav2BridgeError as exc:
        return {
            "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
            "revalidation_status": "blocked",
            "blocking_reasons": [
                f"nav2_recovery_candidate_revalidation_failed:{type(exc).__name__}"
            ],
            "error": str(exc)[:400],
            "dispatch_request_sent": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    evaluated = [
        item
        for item in evaluation.get("candidate_evaluations") or []
        if isinstance(item, Mapping)
    ]
    by_id = {str(item.get("candidate_id") or ""): item for item in evaluated}
    validated = (
        bool(candidates)
        and bool(evaluation.get("global_costmap_snapshot_hash"))
        and bool(evaluation.get("local_costmap_snapshot_hash"))
        and all(
            (item := by_id.get(candidate["candidate_id"])) is not None
            and item.get("path_valid") is True
            and item.get("core_action_feasibility_status")
            == "verified_feasible"
            and math.isclose(float(item.get("x_m")), candidate["x_m"], abs_tol=1e-6)
            and math.isclose(float(item.get("y_m")), candidate["y_m"], abs_tol=1e-6)
            for candidate in candidates
        )
    )
    return {
        "schema_version": "missionos_nav2_recovery_candidate_revalidation.v1",
        "revalidation_status": "validated" if validated else "blocked",
        "approved_candidates": candidates,
        "candidate_evaluations": [dict(item) for item in evaluated],
        "costmap_snapshot_hash": evaluation.get("costmap_snapshot_hash"),
        "global_costmap_snapshot_hash": evaluation.get(
            "global_costmap_snapshot_hash"
        ),
        "local_costmap_snapshot_hash": evaluation.get(
            "local_costmap_snapshot_hash"
        ),
        "core_adapter_id": evaluation.get("core_adapter_id"),
        "core_hazard_state": evaluation.get("core_hazard_state"),
        "core_hazard_state_sha256": evaluation.get(
            "core_hazard_state_sha256"
        ),
        "core_policy_binding": evaluation.get("core_policy_binding"),
        "core_action_feasibility_statuses": [
            by_id.get(candidate["candidate_id"], {}).get(
                "core_action_feasibility_status"
            )
            for candidate in candidates
        ],
        "original_costmap_snapshot_hash": binding.get("costmap_snapshot_hash"),
        "path_sha256_sequence": [
            by_id.get(candidate["candidate_id"], {}).get("path_sha256")
            for candidate in candidates
        ],
        "original_path_sha256_sequence": binding.get("path_sha256_sequence"),
        "blocking_reasons": [] if validated else ["approved_recovery_path_invalid"],
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "command_ack_observed": False,
        "physical_execution_invoked": False,
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
    robot_profile = _robot_profile_from_proposal(proposal)
    profile_spec = _robot_profile_spec(robot_profile)
    approval = {
        "schema_version": TURTLEBOT3_HOME_MISSION_APPROVAL_SCHEMA,
        "approval_status": "approved",
        "operator_approved": True,
        "operator_approval_ref": approval_ref,
        "approval_actor": "missionos_chat_operator",
        "approved_at": approved_at.isoformat(),
        "approved_scope": "bounded_sim_nav2_route_segments",
        "approved_action": "nav2_goal_pose",
        "robot_profile": robot_profile,
        "robot_label": profile_spec["robot_label"],
        "robot_model": str(proposal.get("robot_model") or profile_spec["robot_model"]),
        "execution_target": profile_spec["execution_target"],
        "runtime_substrate": profile_spec["runtime_substrate"],
        "runtime_profile": profile_spec["runtime_profile"],
        "autonomy_envelope": dict(autonomy_envelope),
        "route_authority": build_turtlebot3_route_authority_binding(
            proposal_id=str(
                proposal.get("proposal_id") or "turtlebot3_home_mission"
            ),
            operator_approval_ref=approval_ref,
            approved_scope="bounded_sim_nav2_route_segments",
            planned_segments=[
                dict(segment)
                for segment in proposal.get("planned_segments") or ()
                if isinstance(segment, Mapping)
            ],
            autonomy_envelope=autonomy_envelope,
        ),
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
    """Compatibility wrapper for the extracted execution boundary."""

    return _project_robot_motion(responses)


def _turtlebot3_sidecar_motion_artifacts(
    *,
    bridge_motion: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], bool]:
    """Compatibility wrapper for source-backed sidecar re-observation."""

    return _build_sidecar_motion_artifacts(bridge_motion=bridge_motion)


def _obstacle_observation_from_responses(
    responses: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Compatibility wrapper for extracted obstacle re-observation."""

    return _project_obstacle_observation(responses)


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
    if not math.isfinite(float(x_m)) or not math.isfinite(float(y_m)):
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
    for container_index, container in enumerate(sources):
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
                sample["trajectory_sample_collection"] = key
                sample["trajectory_container_index"] = container_index
                sample["observed_trajectory_evidence_eligible"] = key in {
                    "trajectory_samples",
                    "pose_samples",
                    "odom_samples",
                    "position_samples",
                }
                sample["observation_provenance"] = (
                    "bridge_observed_trajectory_sample"
                    if sample["observed_trajectory_evidence_eligible"]
                    else "ambiguous_bridge_path_sample"
                )
                samples.append(sample)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[float, float, Any, str, str]] = set()
    for index, sample in enumerate(samples):
        key = (
            round(float(sample["x_m"]), 4),
            round(float(sample["y_m"]), 4),
            sample.get("sample_index", index),
            str(sample.get("frame_id") or ""),
            str(sample.get("trajectory_sample_collection") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        if sample.get("sample_index") is None:
            sample["sample_index"] = index
        deduped.append(sample)
    return deduped


def _raw_map_observed_trajectory_points(
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    def _finite_xy(point: Mapping[str, Any]) -> bool:
        x_m = point.get("x_m")
        y_m = point.get("y_m")
        return (
            isinstance(x_m, (int, float))
            and not isinstance(x_m, bool)
            and isinstance(y_m, (int, float))
            and not isinstance(y_m, bool)
            and math.isfinite(float(x_m))
            and math.isfinite(float(y_m))
        )

    eligible = [
        point
        for point in points
        if str(point.get("frame_id") or "").lower() == "map"
        and point.get("observed_trajectory_evidence_eligible") is True
        and point.get("display_alignment_applied") is not True
        and _finite_xy(point)
    ]
    path_samples_excluded = sum(
        1
        for point in points
        if str(point.get("frame_id") or "").lower() == "map"
        and point.get("trajectory_sample_collection") == "path_samples"
    )
    ineligible_map_samples_excluded = sum(
        1
        for point in points
        if str(point.get("frame_id") or "").lower() == "map"
        and point.get("observed_trajectory_evidence_eligible") is not True
    )
    display_aligned_samples_excluded = sum(
        1
        for point in points
        if point.get("display_alignment_applied") is True
    )
    non_map_samples_excluded = sum(
        1
        for point in points
        if str(point.get("frame_id") or "").lower() != "map"
    )
    invalid_numeric_samples_excluded = sum(
        1 for point in points if not _finite_xy(point)
    )
    return eligible, {
        "input_sample_count": len(points),
        "raw_map_frame_sample_count": len(eligible),
        "non_map_frame_sample_count_excluded": non_map_samples_excluded,
        "ineligible_map_frame_sample_count_excluded": (
            ineligible_map_samples_excluded
        ),
        "path_sample_count_excluded": path_samples_excluded,
        "display_aligned_sample_count_excluded": display_aligned_samples_excluded,
        "invalid_numeric_sample_count_excluded": invalid_numeric_samples_excluded,
    }


def _observed_trajectory_group_key(
    point: Mapping[str, Any],
) -> tuple[str, str, str, str, str, str]:
    return (
        str(point.get("segment_ref") or "segment"),
        str(point.get("bridge_response_index") or 0),
        str(point.get("trajectory_container_index") or 0),
        str(point.get("trajectory_sample_collection") or "unknown_collection"),
        str(point.get("observation_provenance") or "unknown_provenance"),
        str(point.get("source") or "unknown_source"),
    )


def _group_observed_trajectory_points(
    points: list[dict[str, Any]],
) -> list[tuple[tuple[str, str, str, str, str, str], list[dict[str, Any]]]]:
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
    for point in points:
        groups.setdefault(_observed_trajectory_group_key(point), []).append(point)
    return list(groups.items())


def _observed_points_from_action_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    segment_ref = str(result.get("segment_ref") or "segment")
    for response_index, response in enumerate(result.get("bridge_responses") or ()):
        if not isinstance(response, Mapping):
            continue
        for sample in _trajectory_samples_from_response(response):
            points.append(
                {
                    **sample,
                    "bridge_response_index": response_index,
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
                "collision_z_m": scene_marker.get("collision_z_m"),
                "collision_size_x_m": scene_marker.get("collision_size_x_m"),
                "collision_size_y_m": scene_marker.get("collision_size_y_m"),
                "collision_size_z_m": scene_marker.get("collision_size_z_m"),
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


def _turtlebot3_obstacle_collision_volumes(
    markers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only complete SDF-backed obstacle AABBs in the map frame."""

    volumes: list[dict[str, Any]] = []
    for marker in markers:
        values = {
            "x_m": _float_or_none(marker.get("x_m")),
            "y_m": _float_or_none(marker.get("y_m")),
            "z_m": _float_or_none(marker.get("collision_z_m")),
            "size_x_m": _float_or_none(marker.get("collision_size_x_m")),
            "size_y_m": _float_or_none(marker.get("collision_size_y_m")),
            "size_z_m": _float_or_none(marker.get("collision_size_z_m")),
        }
        if any(value is None for value in values.values()):
            continue
        if any(
            float(values[key] or 0.0) <= 0.0
            for key in ("size_x_m", "size_y_m", "size_z_m")
        ):
            continue
        volumes.append(
            {
                "obstacle_ref": str(marker.get("name") or ""),
                **values,
                "frame_id": "map",
                "geometry_source": (
                    "opt_in_turtlebot3_gazebo_sdf_collision_volume"
                ),
                "semantic_candidate": str(marker.get("label") or "") or None,
                "evidence_ref": str(marker.get("source") or "") or None,
            }
        )
    return volumes


def _collision_envelope_for_profile(
    robot_profile: Any,
) -> dict[str, Any] | None:
    if normalize_turtlebot_nav2_robot_profile(robot_profile) != "turtlebot3":
        return None
    return dict(_TURTLEBOT3_STOCK_COLLISION_ENVELOPE)


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
    observed_points: list[dict[str, Any]],
    recovery_points: list[dict[str, Any]],
    visual_observations: Sequence[Mapping[str, Any]] = (),
    robot_profile: Any = "turtlebot3",
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
            "obstacle_trajectory_geometry_status": "not_required",
            "obstacle_trajectory_geometry_frame_id": None,
            "obstacle_trajectory_raw_map_frame_sample_count": 0,
            "obstacle_trajectory_observed_stream_count": 0,
            "obstacle_trajectory_observed_segment_count": 0,
            "obstacle_trajectory_non_map_sample_count_excluded": 0,
            "obstacle_trajectory_ineligible_map_sample_count_excluded": 0,
            "obstacle_trajectory_path_sample_count_excluded": 0,
            "obstacle_trajectory_display_aligned_sample_count_excluded": 0,
            "obstacle_trajectory_invalid_numeric_sample_count_excluded": 0,
            "obstacle_trajectory_display_alignment_used": False,
            "obstacle_trajectory_3d_clearance": {},
            "obstacle_trajectory_3d_clearance_observed": False,
            "obstacle_trajectory_3d_collision_observed": False,
            "obstacle_trajectory_3d_clearance_status": "not_required",
        }
    raw_map_points, evidence_filter = _raw_map_observed_trajectory_points(
        [*observed_points, *recovery_points]
    )
    xy_points: list[tuple[float, float]] = []
    xy_points_by_observed_stream: dict[
        tuple[str, str, str, str, str, str], list[tuple[float, float]]
    ] = {}
    grouped_points = list(_group_observed_trajectory_points(raw_map_points))
    for group_key, group_points in grouped_points:
        for point in group_points:
            x_m = _float_or_none(point.get("x_m"))
            y_m = _float_or_none(point.get("y_m"))
            if x_m is None or y_m is None:
                continue
            xy = (x_m, y_m)
            xy_points.append(xy)
            xy_points_by_observed_stream.setdefault(group_key, []).append(xy)
    observed_stream_segment_count = sum(
        max(len(stream_points) - 1, 0)
        for stream_points in xy_points_by_observed_stream.values()
    )
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
        for segment_points in xy_points_by_observed_stream.values():
            for start, end in zip(segment_points, segment_points[1:], strict=False):
                if _segment_intersects_rect(start, end, rect):
                    segment_intersections += 1
    intersects = point_intersections > 0 or segment_intersections > 0
    geometry_status = (
        "raw_map_frame_trajectory_unavailable"
        if not xy_points
        else "raw_map_frame_trajectory_insufficient"
        if observed_stream_segment_count == 0
        else "obstacle_geometry_unavailable"
        if not rects
        else "observed"
    )
    visual_volumes, unresolved_visual_refs = (
        visual_observation_collision_candidates(visual_observations)
    )
    clearance_3d = assess_ground_robot_trajectory_clearance_3d(
        trajectory_streams=[
            group_points for _group_key, group_points in grouped_points
        ],
        robot_collision_envelope=_collision_envelope_for_profile(robot_profile),
        # Every complete source-backed scene volume is a collision candidate.
        # Semantic labels are display metadata and never decide inclusion.
        obstacle_volumes=[
            *_turtlebot3_obstacle_collision_volumes(markers),
            *visual_volumes,
        ],
        unresolved_candidate_refs=unresolved_visual_refs,
        base_z_m=0.0,
    )
    return {
        "obstacle_trajectory_clearance_observed": (
            geometry_status == "observed" and not intersects
        ),
        "obstacle_trajectory_intersects_obstacle": intersects,
        "obstacle_intersection_point_count": point_intersections,
        "obstacle_intersection_segment_count": segment_intersections,
        "obstacle_min_clearance_m": round(min_clearance, 6)
        if min_clearance is not None
        else None,
        "obstacle_trajectory_geometry_source": (
            "raw_ros2_nav2_bridge_map_frame_observed_trajectory_vs_obstacle_bbox"
        ),
        "obstacle_trajectory_geometry_status": geometry_status,
        "obstacle_trajectory_geometry_frame_id": "map" if xy_points else None,
        "obstacle_trajectory_raw_map_frame_sample_count": evidence_filter[
            "raw_map_frame_sample_count"
        ],
        "obstacle_trajectory_observed_stream_count": len(
            xy_points_by_observed_stream
        ),
        "obstacle_trajectory_observed_segment_count": observed_stream_segment_count,
        "obstacle_trajectory_non_map_sample_count_excluded": evidence_filter[
            "non_map_frame_sample_count_excluded"
        ],
        "obstacle_trajectory_ineligible_map_sample_count_excluded": evidence_filter[
            "ineligible_map_frame_sample_count_excluded"
        ],
        "obstacle_trajectory_path_sample_count_excluded": evidence_filter[
            "path_sample_count_excluded"
        ],
        "obstacle_trajectory_display_aligned_sample_count_excluded": evidence_filter[
            "display_aligned_sample_count_excluded"
        ],
        "obstacle_trajectory_invalid_numeric_sample_count_excluded": evidence_filter[
            "invalid_numeric_sample_count_excluded"
        ],
        "obstacle_trajectory_display_alignment_used": False,
        "obstacle_trajectory_3d_clearance": clearance_3d.model_dump(mode="json"),
        "obstacle_trajectory_3d_clearance_observed": (
            clearance_3d.clearance_observed
        ),
        "obstacle_trajectory_3d_collision_observed": (
            clearance_3d.collision_observed
        ),
        "obstacle_trajectory_3d_clearance_status": clearance_3d.status,
        "obstacle_trajectory_geometry_claim_boundary": (
            "Obstacle-avoidance completion requires the bridge observation plus "
            "an explicitly observed raw map-frame trajectory that does not intersect "
            "the obstacle bbox. Display alignment and ambiguous path_samples are "
            "visualization-only and cannot support this simulator completion claim; "
            "segments are formed only within one coherent collection, provenance, "
            "and source stream."
        ),
    }


def _recovery_requested_side_observation(
    *,
    checkpoint: Mapping[str, Any],
    approved_recovery_results: list[dict[str, Any]],
) -> dict[str, Any]:
    geometry = checkpoint.get("recovery_revision_geometry")
    geometry = geometry if isinstance(geometry, Mapping) else {}
    requested_direction = str(geometry.get("requested_direction") or "")
    if requested_direction not in {"left", "right"}:
        return {
            "schema_version": (
                "missionos_turtlebot3_recovery_requested_side_observation.v1"
            ),
            "observation_status": "not_required",
            "requested_direction": requested_direction or None,
            "requested_side_observed": False,
            "raw_map_frame_sample_count": 0,
            "non_map_frame_sample_count_excluded": 0,
            "ineligible_map_frame_sample_count_excluded": 0,
            "path_sample_count_excluded": 0,
            "display_aligned_sample_count_excluded": 0,
            "invalid_numeric_sample_count_excluded": 0,
            "observed_trajectory_stream_count": 0,
            "display_alignment_used": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
    left_normal = geometry.get("left_normal_unit")
    route_direction = geometry.get("route_direction_unit")
    obstacle = geometry.get("obstacle")
    left_normal = left_normal if isinstance(left_normal, Mapping) else {}
    route_direction = (
        route_direction if isinstance(route_direction, Mapping) else {}
    )
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    normal_x = _revision_numeric(left_normal.get("x"))
    normal_y = _revision_numeric(left_normal.get("y"))
    direction_x = _revision_numeric(route_direction.get("x"))
    direction_y = _revision_numeric(route_direction.get("y"))
    obstacle_x = _revision_numeric(obstacle.get("x_m"))
    obstacle_y = _revision_numeric(obstacle.get("y_m"))
    size_x = _revision_numeric(obstacle.get("size_x_m"))
    size_y = _revision_numeric(obstacle.get("size_y_m"))
    if None in (
        normal_x,
        normal_y,
        direction_x,
        direction_y,
        obstacle_x,
        obstacle_y,
        size_x,
        size_y,
    ):
        return {
            "schema_version": (
                "missionos_turtlebot3_recovery_requested_side_observation.v1"
            ),
            "observation_status": "source_geometry_invalid",
            "requested_direction": requested_direction,
            "requested_side_observed": False,
            "raw_map_frame_sample_count": 0,
            "non_map_frame_sample_count_excluded": 0,
            "ineligible_map_frame_sample_count_excluded": 0,
            "path_sample_count_excluded": 0,
            "display_aligned_sample_count_excluded": 0,
            "invalid_numeric_sample_count_excluded": 0,
            "observed_trajectory_stream_count": 0,
            "display_alignment_used": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
    side_sign = 1.0 if requested_direction == "left" else -1.0
    obstacle_perpendicular_support = (
        abs(float(normal_x)) * float(size_x) / 2.0
        + abs(float(normal_y)) * float(size_y) / 2.0
    )
    required_signed_offset = obstacle_perpendicular_support + float(
        geometry.get("wide_bbox_clearance_m")
        or _TURTLEBOT3_RECOVERY_REVISION_WIDE_BBOX_CLEARANCE_M
    )
    obstacle_parallel_support = (
        abs(float(direction_x)) * float(size_x) / 2.0
        + abs(float(direction_y)) * float(size_y) / 2.0
    )
    segment_observations: list[dict[str, Any]] = []
    total_sample_count = 0
    total_non_map_sample_count_excluded = 0
    total_ineligible_map_sample_count_excluded = 0
    total_path_sample_count_excluded = 0
    total_display_aligned_sample_count_excluded = 0
    total_invalid_numeric_sample_count_excluded = 0

    def _stream_observation(
        *,
        result: Mapping[str, Any],
        group_key: tuple[str, str, str, str, str, str],
        raw_map_points: list[dict[str, Any]],
        evidence_filter: Mapping[str, int],
    ) -> dict[str, Any]:
        def _longitudinal(point: Mapping[str, Any]) -> float:
            return (
                (float(point["x_m"]) - float(obstacle_x)) * float(direction_x)
                + (float(point["y_m"]) - float(obstacle_y)) * float(direction_y)
            )

        def _requested_side_offset(point: Mapping[str, Any]) -> float:
            return side_sign * (
                (float(point["x_m"]) - float(obstacle_x)) * float(normal_x)
                + (float(point["y_m"]) - float(obstacle_y)) * float(normal_y)
            )

        abreast_offsets = [
            _requested_side_offset(point)
            for point in raw_map_points
            if abs(_longitudinal(point)) <= obstacle_parallel_support + 1e-9
        ]
        longitudinal_samples = [_longitudinal(point) for point in raw_map_points]
        crossed_low_to_high = False
        crossed_high_to_low = False
        low_observed = False
        high_observed = False
        for longitudinal in longitudinal_samples:
            if longitudinal <= -obstacle_parallel_support + 1e-9:
                low_observed = True
                if high_observed:
                    crossed_high_to_low = True
            if longitudinal >= obstacle_parallel_support - 1e-9:
                high_observed = True
                if low_observed:
                    crossed_low_to_high = True
        full_longitudinal_crossing_observed = (
            crossed_low_to_high or crossed_high_to_low
        )
        for start, end in zip(
            raw_map_points,
            raw_map_points[1:],
            strict=False,
        ):
            start_longitudinal = _longitudinal(start)
            end_longitudinal = _longitudinal(end)
            lower = max(
                min(start_longitudinal, end_longitudinal),
                -obstacle_parallel_support,
            )
            upper = min(
                max(start_longitudinal, end_longitudinal),
                obstacle_parallel_support,
            )
            if lower > upper + 1e-9:
                continue
            longitudinal_delta = end_longitudinal - start_longitudinal
            if abs(longitudinal_delta) <= 1e-12:
                continue
            clipped_longitudinals = (
                (lower,)
                if math.isclose(lower, upper, abs_tol=1e-12)
                else (lower, upper)
            )
            for clipped_longitudinal in clipped_longitudinals:
                ratio = (
                    clipped_longitudinal - start_longitudinal
                ) / longitudinal_delta
                interpolated = {
                    "x_m": float(start["x_m"])
                    + ratio * (float(end["x_m"]) - float(start["x_m"])),
                    "y_m": float(start["y_m"])
                    + ratio * (float(end["y_m"]) - float(start["y_m"])),
                }
                abreast_offsets.append(_requested_side_offset(interpolated))
        maximum_requested_side_offset = max(abreast_offsets, default=None)
        minimum_requested_side_offset = min(abreast_offsets, default=None)
        return {
            "segment_ref": group_key[0] or result.get("segment_ref"),
            "bridge_response_index": group_key[1],
            "trajectory_container_index": group_key[2],
            "trajectory_sample_collection": group_key[3],
            "observation_provenance": group_key[4],
            "observation_source": group_key[5],
            "raw_map_frame_sample_count": len(raw_map_points),
            "non_map_frame_sample_count_excluded": evidence_filter[
                "non_map_frame_sample_count_excluded"
            ],
            "ineligible_map_frame_sample_count_excluded": evidence_filter[
                "ineligible_map_frame_sample_count_excluded"
            ],
            "path_sample_count_excluded": evidence_filter[
                "path_sample_count_excluded"
            ],
            "display_aligned_sample_count_excluded": evidence_filter[
                "display_aligned_sample_count_excluded"
            ],
            "invalid_numeric_sample_count_excluded": evidence_filter[
                "invalid_numeric_sample_count_excluded"
            ],
            "obstacle_abreast_evidence_count": len(abreast_offsets),
            "full_longitudinal_crossing_observed": (
                full_longitudinal_crossing_observed
            ),
            "maximum_requested_side_offset_m": (
                round(maximum_requested_side_offset, 6)
                if maximum_requested_side_offset is not None
                else None
            ),
            "minimum_requested_side_offset_m": (
                round(minimum_requested_side_offset, 6)
                if minimum_requested_side_offset is not None
                else None
            ),
            "requested_side_observed": (
                full_longitudinal_crossing_observed
                and minimum_requested_side_offset is not None
                and minimum_requested_side_offset + 1e-6
                >= required_signed_offset
            ),
        }

    for result in approved_recovery_results:
        raw_map_points, evidence_filter = _raw_map_observed_trajectory_points(
            _observed_points_from_action_result(result)
        )
        total_sample_count += len(raw_map_points)
        total_non_map_sample_count_excluded += evidence_filter[
            "non_map_frame_sample_count_excluded"
        ]
        total_ineligible_map_sample_count_excluded += evidence_filter[
            "ineligible_map_frame_sample_count_excluded"
        ]
        total_path_sample_count_excluded += evidence_filter[
            "path_sample_count_excluded"
        ]
        total_display_aligned_sample_count_excluded += evidence_filter[
            "display_aligned_sample_count_excluded"
        ]
        total_invalid_numeric_sample_count_excluded += evidence_filter[
            "invalid_numeric_sample_count_excluded"
        ]
        observed_streams = _group_observed_trajectory_points(raw_map_points)
        if not observed_streams:
            observed_streams = [
                (
                    (
                        str(result.get("segment_ref") or "segment"),
                        "unavailable",
                        "unavailable",
                        "unavailable",
                        "unavailable",
                        "unavailable",
                    ),
                    [],
                )
            ]
        for group_key, stream_points in observed_streams:
            segment_observations.append(
                _stream_observation(
                    result=result,
                    group_key=group_key,
                    raw_map_points=stream_points,
                    evidence_filter=evidence_filter,
                )
            )
    crossing_segment_observations = [
        item
        for item in segment_observations
        if item["full_longitudinal_crossing_observed"] is True
    ]
    requested_side_observed = bool(crossing_segment_observations) and all(
        item["requested_side_observed"] is True
        for item in crossing_segment_observations
    )
    return {
        "schema_version": (
            "missionos_turtlebot3_recovery_requested_side_observation.v1"
        ),
        "observation_status": (
            "observed"
            if requested_side_observed
            else "raw_map_frame_trajectory_unavailable"
            if total_sample_count == 0
            else "not_observed"
        ),
        "requested_direction": requested_direction,
        "required_signed_offset_m": round(required_signed_offset, 6),
        "obstacle_longitudinal_half_window_m": round(
            obstacle_parallel_support,
            6,
        ),
        "approved_recovery_segment_count": len(approved_recovery_results),
        "segment_observations": segment_observations,
        "raw_map_frame_sample_count": total_sample_count,
        "non_map_frame_sample_count_excluded": (
            total_non_map_sample_count_excluded
        ),
        "ineligible_map_frame_sample_count_excluded": (
            total_ineligible_map_sample_count_excluded
        ),
        "path_sample_count_excluded": total_path_sample_count_excluded,
        "display_aligned_sample_count_excluded": (
            total_display_aligned_sample_count_excluded
        ),
        "invalid_numeric_sample_count_excluded": (
            total_invalid_numeric_sample_count_excluded
        ),
        "observed_trajectory_stream_count": sum(
            1
            for item in segment_observations
            if item["trajectory_sample_collection"] != "unavailable"
        ),
        "observation_source": (
            "raw_ros2_nav2_bridge_map_frame_observed_trajectory_samples_"
            "abreast_of_obstacle"
        ),
        "requested_side_observed": requested_side_observed,
        "display_alignment_used": False,
        "claim_boundary": (
            "Only raw map-frame bridge samples may verify requested-side traversal; "
            "display alignment, odom-frame fallback, and ambiguous path_samples "
            "are excluded. Crossing interpolation is confined to one coherent "
            "sample collection, provenance, and source stream."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
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
    approved_recovery_segment_results: list[dict[str, Any]],
    subsequent_recovery_segment_results: list[dict[str, Any]],
    status: str,
    obstacle_required: bool,
    obstacle: Mapping[str, Any],
    motion: Mapping[str, Any],
    runtime_recovery_triggered: bool,
    recovery_action_suggested: str | None,
    route_resumed_after_recovery: bool,
    visual_observations: list[dict[str, Any]] | None = None,
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
    approved_recovery_results = (
        approved_recovery_segment_results
        if approved_recovery_segment_results
        else ([dict(recovery_segment_result)] if recovery_segment_result else [])
    )
    recovery_points = [
        point
        for result in approved_recovery_results
        for point in _observed_points_from_action_result(result)
    ]
    subsequent_recovery_points = [
        point
        for result in subsequent_recovery_segment_results
        for point in _observed_points_from_action_result(result)
    ]
    display_alignment = _observed_display_alignment(
        planned_points=planned_points,
        observed_points=observed_points,
        recovery_points=[*recovery_points, *subsequent_recovery_points],
    )
    observed_points = _apply_observed_display_alignment(
        observed_points,
        alignment=display_alignment,
    )
    recovery_points = _apply_observed_display_alignment(
        recovery_points,
        alignment=display_alignment,
    )
    subsequent_recovery_points = _apply_observed_display_alignment(
        subsequent_recovery_points,
        alignment=display_alignment,
    )
    observed_points, display_sanitize = _sanitize_observed_display_points(
        observed_points
    )
    recovery_points, _recovery_sanitize = _sanitize_observed_display_points(
        recovery_points
    )
    subsequent_recovery_points, _subsequent_recovery_sanitize = (
        _sanitize_observed_display_points(subsequent_recovery_points)
    )
    observed_points, display_decimation = _decimate_observed_display_points(
        observed_points
    )
    display_decimation = {**display_decimation, "sanitize": display_sanitize}
    recovery_points, _recovery_decimation = _decimate_observed_display_points(
        recovery_points
    )
    subsequent_recovery_points, _subsequent_recovery_decimation = (
        _decimate_observed_display_points(subsequent_recovery_points)
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
    subsequent_recovery_targets = [
        {
            "x_m": goal.get("x_m"),
            "y_m": goal.get("y_m"),
            "yaw_rad": goal.get("yaw_rad"),
            "frame_id": goal.get("frame_id"),
            "label": goal.get("label"),
            "role": "subsequent_recovery_target",
            "source": "missionos_subsequent_recovery_nav2_segment",
        }
        for result in subsequent_recovery_segment_results
        if isinstance((goal := result.get("goal_pose")), Mapping)
    ]
    approved_recovery_targets = [
        {
            "x_m": goal.get("x_m"),
            "y_m": goal.get("y_m"),
            "yaw_rad": goal.get("yaw_rad"),
            "frame_id": goal.get("frame_id"),
            "label": goal.get("label"),
            "role": "approved_recovery_target",
            "source": "missionos_approved_recovery_nav2_segment",
        }
        for result in approved_recovery_results
        if isinstance((goal := result.get("goal_pose")), Mapping)
    ]

    obstacles = _turtlebot3_delivery_obstacle_markers(
        obstacle_required=obstacle_required,
        obstacle=obstacle,
    )
    visual_observation_records = [
        dict(record)
        for record in (visual_observations or [])
        if isinstance(record, Mapping)
    ]
    projected_visual_points = [
        {
            "x_m": projection.get("x_m"),
            "y_m": projection.get("y_m"),
        }
        for record in visual_observation_records
        if isinstance((projection := record.get("map_projection")), Mapping)
        and projection.get("status") == "projected"
        and isinstance(projection.get("x_m"), (int, float))
        and isinstance(projection.get("y_m"), (int, float))
    ]
    robot_profile = _robot_profile_from_proposal(proposal)
    floor_plan = _turtlebot3_home_floor_plan(robot_profile)
    floor_bounds = floor_plan["bounds"]

    all_x = [
        float(point["x_m"])
        for point in [
            *planned_points,
            *observed_points,
            *recovery_points,
            *subsequent_recovery_points,
            *obstacles,
            *projected_visual_points,
        ]
        if isinstance(point.get("x_m"), (int, float))
        and not isinstance(point.get("x_m"), bool)
    ]
    all_y = [
        float(point["y_m"])
        for point in [
            *planned_points,
            *observed_points,
            *recovery_points,
            *subsequent_recovery_points,
            *obstacles,
            *projected_visual_points,
        ]
        if isinstance(point.get("y_m"), (int, float))
        and not isinstance(point.get("y_m"), bool)
    ]
    min_x = min(float(floor_bounds["min_x_m"]), min(all_x, default=-2.5))
    max_x = max(float(floor_bounds["max_x_m"]), max(all_x, default=1.0))
    min_y = min(float(floor_bounds["min_y_m"]), min(all_y, default=-1.0))
    max_y = max(float(floor_bounds["max_y_m"]), max(all_y, default=1.0))
    current_pose = (
        subsequent_recovery_points[-1] if subsequent_recovery_points else None
    )
    if current_pose is None and route_resumed_after_recovery and observed_points:
        current_pose = observed_points[-1]
    if current_pose is None and recovery_points:
        current_pose = recovery_points[-1]
    if current_pose is None and observed_points:
        current_pose = observed_points[-1]
    profile_spec = _robot_profile_spec(robot_profile)

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
        "visual_observations": visual_observation_records,
        "visual_observation_layer": {
            "source": "missionos_camera_lidar_perception_binding",
            "count": len(visual_observation_records),
            "claim_boundary": (
                "Camera+LiDAR observation evidence only. A corroborated marker "
                "means the exact camera frame, a live VLM claim, and an "
                "independent LiDAR candidate shared one decision epoch; it is "
                "not scene ground truth and creates no approval, dispatch, or "
                "delivery claim."
            ),
        },
        "trajectory_clearance_3d": dict(
            obstacle.get("obstacle_trajectory_3d_clearance") or {}
        ),
        "recovery": {
            "triggered": runtime_recovery_triggered,
            "selected_action": recovery_action_suggested,
            "target": recovery_target,
            "approved_targets": approved_recovery_targets,
            "observed_points": recovery_points,
            "completion_claimed": bool(approved_recovery_results)
            and all(
                result.get("completion_claimed") is True
                for result in approved_recovery_results
            ),
            "subsequent_targets": subsequent_recovery_targets,
            "subsequent_observed_points": subsequent_recovery_points,
            "subsequent_completion_claimed": any(
                result.get("completion_claimed") is True
                for result in subsequent_recovery_segment_results
            ),
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


def _capture_camera_perception_observation(
    *, decision_epoch_ref: str
) -> tuple[
    dict[str, Any] | None, dict[str, Any]
]:
    """Capture one camera frame and classify it into a camera observation.

    Returns ``(observation_payload | None, pipeline_record)``. Fail-open at
    every stage: the default ``burger`` simulation profile has no camera,
    while the opt-in ``waffle_pi`` profile publishes RGB and LaserScan topics
    even under Xvfb. If either source is absent, recovery proceeds without
    progressive camera support and the pipeline records the exact boundary.
    """

    record: dict[str, Any] = {
        "schema_version": TURTLEBOT3_CAMERA_PERCEPTION_PIPELINE_SCHEMA_VERSION,
        "pipeline_status": "not_enabled",
        "decision_epoch_ref": decision_epoch_ref,
        "capture": {},
        "sidecar_status": "",
        "sidecar_blocking_reasons": [],
        "claim_produced": False,
        "claim_boundary": (
            "This record documents an observation pipeline only. Captured "
            "frames and sidecar classifications are evidence for recovery "
            "deliberation; they never create approval, dispatch, or "
            "completion claims."
        ),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    if os.environ.get(TURTLEBOT3_CAMERA_PERCEPTION_ENABLED_ENV) != "1":
        return None, record

    client = Ros2Nav2BridgeCommandClient()
    try:
        response = client.capture_camera_frame()
    except Ros2Nav2BridgeError as exc:
        record["pipeline_status"] = "capture_blocked"
        record["capture"] = {"bridge_error": str(exc)}
        return None, record
    record["capture"] = {
        "camera_frame_captured": response.get("camera_frame_captured") is True,
        "camera_frame_path": str(response.get("camera_frame_path") or ""),
        "camera_frame_sha256": str(response.get("camera_frame_sha256") or ""),
        "camera_topic": str(response.get("camera_topic") or ""),
        "ack_status": str(response.get("ack_status") or ""),
        "blocking_reasons": [
            str(reason) for reason in (response.get("blocking_reasons") or ())
        ],
        "camera_lidar_observation": dict(
            response.get("camera_lidar_observation") or {}
        ),
    }
    if response.get("camera_frame_captured") is not True:
        record["pipeline_status"] = "capture_blocked"
        return None, record

    from src.intelligence.turtlebot3_perception_sidecar import (
        run_turtlebot3_perception_sidecar,
    )

    sidecar_result = run_turtlebot3_perception_sidecar(
        image_path=record["capture"]["camera_frame_path"],
    )
    record["sidecar_status"] = str(sidecar_result.get("sidecar_status") or "")
    record["sidecar_blocking_reasons"] = [
        str(reason) for reason in (sidecar_result.get("blocking_reasons") or ())
    ]
    record["llm_invocation_evidence"] = dict(
        sidecar_result.get("llm_invocation_evidence") or {}
    )
    if record["sidecar_status"] != "classified":
        record["pipeline_status"] = f"sidecar_{record['sidecar_status'] or 'blocked'}"
        return None, record
    observation = dict(sidecar_result.get("camera_observation") or {})
    record["pipeline_status"] = "classified"
    record["claim_produced"] = True
    return observation, record


def _dispatch_harness_stop(
    *,
    reflex: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper around the extracted harness executor."""

    return _dispatch_nav2_harness_stop(
        reflex=reflex,
        mission_ref=str(
            proposal.get("proposal_id") or "turtlebot3_home_mission"
        ),
    )


def _dispatch_nav2_goal(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    goal: Nav2GoalPose,
    approval_ref: str,
    dispatched_at: datetime,
    action_ref_suffix: str,
    publish_initialpose: bool,
    simulate_cancel_after_accept: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper around the extracted bounded executor."""

    mission_contract = None
    if (
        _robot_profile_from_proposal(proposal) == "turtlebot3"
        and action_ref_suffix.startswith("segment_")
    ):
        mission_contract = build_nav2_turtlebot3_runtime_contract(
            proposal_id=str(
                proposal.get("proposal_id") or "turtlebot3_home_mission"
            ),
            action_ref_suffix=action_ref_suffix,
            goal=goal,
        )

    result = _dispatch_concrete_nav2_goal(
        proposal_id=str(
            proposal.get("proposal_id") or "turtlebot3_home_mission"
        ),
        approval_actor=str(
            approval.get("approval_actor") or "missionos_chat_operator"
        ),
        goal=goal,
        approval_ref=approval_ref,
        dispatched_at=dispatched_at,
        action_ref_suffix=action_ref_suffix,
        raw_logs_ref=_turtlebot3_raw_logs_ref_from_env(
            _robot_profile_from_proposal(proposal)
        ),
        publish_initialpose=publish_initialpose,
        simulate_cancel_after_accept=simulate_cancel_after_accept,
    )
    if mission_contract is None:
        return result

    predicate_evaluation = evaluate_nav2_turtlebot3_runtime_result(
        contract=mission_contract,
        goal=goal,
        action_result=result,
        evaluated_at=datetime.now(timezone.utc),
    )
    predicate_completion_claimed = (
        predicate_evaluation.get("completion_claimed") is True
    )
    blocking_reasons = list(result.get("blocking_reasons") or ())
    if (
        result.get("completion_claimed") is True
        and not predicate_completion_claimed
    ):
        blocking_reasons.append(
            "mission_contract_predicate_not_satisfied"
        )
        blocking_reasons.extend(
            str(reason)
            for reason in predicate_evaluation.get("reasons") or ()
        )
    return {
        **result,
        "adapter_completion_claimed": (
            result.get("completion_claimed") is True
        ),
        "adapter_completion_scope": (
            str(result.get("completion_scope") or "none")
        ),
        "mission_contract": json.loads(
            json.dumps(
                mission_contract.to_material(),
                ensure_ascii=True,
                sort_keys=True,
            )
        ),
        "mission_contract_sha256": mission_contract.contract_sha256,
        "mission_contract_predicate_evaluation": predicate_evaluation,
        "completion_claimed": predicate_completion_claimed,
        "completion_scope": (
            "sim_action" if predicate_completion_claimed else "none"
        ),
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
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


def _recovery_approved_parameters(
    *,
    selected_action: str,
    recovery_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    if selected_action == "return_home":
        home = _profile_home_pose()
        return {
            "target_x_m": home.x_m,
            "target_y_m": home.y_m,
            "return_home_required": True,
        }
    if selected_action != "avoid_obstacle":
        return {}
    observations = recovery_proposal.get("input_observations")
    observations = observations if isinstance(observations, Mapping) else {}
    target_x = observations.get("recommended_avoidance_target_x_m")
    target_y = observations.get("recommended_avoidance_target_y_m")
    if (
        not isinstance(target_x, (int, float))
        or isinstance(target_x, bool)
        or not math.isfinite(float(target_x))
        or not isinstance(target_y, (int, float))
        or isinstance(target_y, bool)
        or not math.isfinite(float(target_y))
    ):
        return {}
    return {
        "target_x_m": float(target_x),
        "target_y_m": float(target_y),
        "obstacle_avoidance_required": True,
    }


def _build_turtlebot3_recovery_checkpoint(
    *,
    proposal: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    segment_results: list[dict[str, Any]],
    recovery_proposals: tuple[Mapping[str, Any], ...],
    recovery_proposal_classifications: tuple[Mapping[str, Any], ...],
    recovery_planner_result: Mapping[str, Any],
    runtime_recovery_obstacle_scenario: Mapping[str, Any],
    runtime_recovery_motion_context: Mapping[str, Any],
    completed_segment_index: int,
    route_failure_observation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    recovery_proposal = recovery_proposals[0] if recovery_proposals else {}
    classification = (
        recovery_proposal_classifications[0]
        if recovery_proposal_classifications
        else {}
    )
    selected_action = str(recovery_proposal.get("selected_action") or "")
    candidate_resolution = runtime_recovery_obstacle_scenario.get(
        "recovery_candidate_resolution"
    )
    candidate_resolution = (
        candidate_resolution if isinstance(candidate_resolution, Mapping) else {}
    )
    selected_candidate = candidate_resolution.get("selected_candidate")
    selected_candidate = (
        selected_candidate if isinstance(selected_candidate, Mapping) else {}
    )
    approved_parameters = _recovery_approved_parameters(
        selected_action=selected_action,
        recovery_proposal=recovery_proposal,
    )
    if selected_action == "avoid_obstacle" and selected_candidate:
        target_yaw_rad = selected_candidate.get(
            "recommended_arrival_yaw_rad",
            selected_candidate.get("yaw_rad", 0.0),
        )
        approved_parameters = {
            "target_x_m": float(selected_candidate["x_m"]),
            "target_y_m": float(selected_candidate["y_m"]),
            "target_yaw_rad": float(target_yaw_rad),
            "obstacle_avoidance_required": True,
        }
    selected_sequence = [
        dict(item)
        for item in candidate_resolution.get("selected_sequence") or []
        if isinstance(item, Mapping)
    ]
    if selected_action == "avoid_obstacle" and len(selected_sequence) == 2:
        approved_parameters = {
            "recovery_waypoints": [
                {
                    "target_x_m": float(item["x_m"]),
                    "target_y_m": float(item["y_m"]),
                }
                for item in selected_sequence
            ],
            "obstacle_avoidance_required": True,
        }
    resume_state = {
        "planned_segments": [goal.model_dump(mode="json") for goal in goals],
        "segment_results": [dict(item) for item in segment_results],
        "route_failure_observation_results": [
            dict(item) for item in (route_failure_observation_results or [])
        ],
        "recovery_proposals": [dict(item) for item in recovery_proposals],
        "recovery_proposal_classifications": [
            dict(item) for item in recovery_proposal_classifications
        ],
        "recovery_planner_result": dict(recovery_planner_result),
        "runtime_recovery_obstacle_scenario": dict(
            runtime_recovery_obstacle_scenario
        ),
        "runtime_recovery_motion_context": dict(runtime_recovery_motion_context),
    }
    checkpoint: dict[str, Any] = {
        "schema_version": TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA,
        "checkpoint_status": "awaiting_operator_approval",
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "robot_profile": _robot_profile_from_proposal(proposal),
        "execution_target": str(proposal.get("execution_target") or ""),
        "recovery_proposal_id": str(recovery_proposal.get("proposal_id") or ""),
        "recovery_classification_id": str(
            classification.get("classification_id") or ""
        ),
        "selected_action": selected_action,
        "approved_parameters": approved_parameters,
        "completed_segment_count": len(segment_results),
        "next_segment_index": completed_segment_index + 1,
        "remaining_segment_count": max(len(goals) - completed_segment_index, 0),
        "planned_segments_sha256": _planned_segments_sha256(goals),
        "resume_state_hash": _recovery_resume_state_hash(resume_state),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    if selected_action == "return_home":
        checkpoint["recovery_goal_poses"] = [
            _profile_home_pose().model_dump(mode="json")
        ]
    elif selected_action == "avoid_obstacle":
        sequence = selected_sequence or ([dict(selected_candidate)] if selected_candidate else [])
        candidate_ids = [str(item.get("candidate_id") or "") for item in sequence]
        if sequence and all(candidate_ids):
            template = _profile_dynamic_obstacle_avoidance_goal()
            if len(selected_sequence) == 2:
                checkpoint["recovery_goal_poses"] = [
                    template.model_copy(
                        update={
                            "x_m": float(item["x_m"]),
                            "y_m": float(item["y_m"]),
                            "yaw_rad": float(
                                item.get(
                                    "recommended_arrival_yaw_rad",
                                    item.get("yaw_rad", 0.0),
                                )
                            ),
                            "label": str(
                                item.get("candidate_id") or template.label
                            ),
                        }
                    ).model_dump(mode="json")
                    for item in sequence
                ]
            checkpoint["recovery_candidate_binding"] = {
                "candidate_id": candidate_ids[-1],
                "path_sha256": sequence[-1].get("path_sha256"),
                "costmap_snapshot_hash": candidate_resolution.get(
                    "costmap_snapshot_hash"
                ),
                "recommended_arrival_yaw_rad": float(
                    sequence[-1].get(
                        "recommended_arrival_yaw_rad",
                        sequence[-1].get("yaw_rad", 0.0),
                    )
                ),
                "live_costmap_validated": candidate_resolution.get(
                    "live_costmap_validated"
                )
                is True,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
            }
            if candidate_resolution.get("core_adapter_id"):
                checkpoint["recovery_candidate_binding"].update(
                    {
                        "core_action_feasibility_required": True,
                        "core_adapter_id": candidate_resolution.get(
                            "core_adapter_id"
                        ),
                        "core_hazard_state": candidate_resolution.get(
                            "core_hazard_state"
                        ),
                        "core_hazard_state_sha256": candidate_resolution.get(
                            "core_hazard_state_sha256"
                        ),
                        "core_policy_binding": candidate_resolution.get(
                            "core_policy_binding"
                        ),
                        "core_action_feasibility_statuses": [
                            item.get("core_action_feasibility_status")
                            for item in sequence
                        ],
                        "core_action_feasibility_artifact_ids": [
                            item["core_action_feasibility"].get(
                                "artifact_id"
                            )
                            for item in sequence
                            if isinstance(
                                item.get("core_action_feasibility"),
                                Mapping,
                            )
                        ],
                    }
                )
            if candidate_resolution.get("dual_costmap_validated") is True:
                checkpoint["recovery_candidate_binding"].update(
                    {
                        "candidate_ids": candidate_ids,
                        "path_sha256_sequence": [
                            item.get("path_sha256") for item in sequence
                        ],
                        "global_costmap_snapshot_hash": (
                            candidate_resolution.get(
                                "global_costmap_snapshot_hash"
                            )
                        ),
                        "local_costmap_snapshot_hash": candidate_resolution.get(
                            "local_costmap_snapshot_hash"
                        ),
                        "dual_costmap_validated": True,
                        "bounded_retreat_required": len(sequence) > 1,
                    }
                )
    if (
        selected_action == "avoid_obstacle"
        and _truthy_env(TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV)
        and candidate_resolution.get("resolution_status") != "validated"
    ):
        checkpoint["operator_guidance_required"] = True
        checkpoint["action_feasibility_status"] = "unverified"
        checkpoint["action_feasibility_blocking_reasons"] = list(
            candidate_resolution.get("blocking_reasons")
            or ["no_core_verified_recovery_candidate"]
        )
    checkpoint["recovery_contract_bundle"] = (
        build_turtlebot3_recovery_contract_bundle(checkpoint)
    )
    checkpoint_hash = _recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    return checkpoint


def _build_recovery_repair_child_checkpoint(
    *,
    parent_checkpoint: Mapping[str, Any],
    proposal: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    segment_results: list[dict[str, Any]],
    candidate_observation_results: list[dict[str, Any]],
    recovery_proposals: tuple[Mapping[str, Any], ...],
    recovery_proposal_classifications: tuple[Mapping[str, Any], ...],
    recovery_planner_result: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any],
    motion_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """Create a proposal-only repair child after a failed recovery dispatch."""

    parent_attempt = int(parent_checkpoint.get("repair_attempt") or 0)
    if parent_attempt >= 2:
        return None
    parent_binding = parent_checkpoint.get("recovery_candidate_binding")
    parent_binding = parent_binding if isinstance(parent_binding, Mapping) else {}
    excluded = {
        str(candidate_id)
        for candidate_id in (
            parent_binding.get("candidate_ids")
            or [parent_binding.get("candidate_id")]
        )
        if str(candidate_id or "")
    }
    resolution = _resolve_recovery_candidate(
        obstacle_scenario,
        segment_results=candidate_observation_results,
        excluded_candidate_ids=excluded,
    )
    if resolution.get("resolution_status") != "validated":
        return None
    child_scenario = {
        **dict(obstacle_scenario),
        "recovery_candidate_resolution": dict(resolution),
        "repair_parent_checkpoint_id": parent_checkpoint.get("checkpoint_id"),
        "repair_parent_failure_reasons": list(
            parent_checkpoint.get("failure_reasons") or []
        ),
    }
    child_planner = {
        **dict(recovery_planner_result),
        "planner_status": "repair_proposed",
        "proposal_source": "deterministic_repair_after_verified_failure",
        "recovery_candidate_resolution": dict(resolution),
        "dispatch_authority_created": False,
        "automatic_redispatch_performed": False,
    }
    child = _build_turtlebot3_recovery_checkpoint(
        proposal=proposal,
        goals=goals,
        segment_results=segment_results,
        recovery_proposals=recovery_proposals,
        recovery_proposal_classifications=recovery_proposal_classifications,
        recovery_planner_result=child_planner,
        runtime_recovery_obstacle_scenario=child_scenario,
        runtime_recovery_motion_context=motion_context,
        completed_segment_index=int(parent_checkpoint.get("next_segment_index") or 1)
        - 1,
    )
    child = {
        **child,
        "parent_checkpoint_id": parent_checkpoint.get("checkpoint_id"),
        "parent_checkpoint_hash": parent_checkpoint.get("checkpoint_hash"),
        "repair_attempt": parent_attempt + 1,
        "repair_trigger": "approved_recovery_verification_failed",
        "excluded_failed_candidate_ids": sorted(excluded),
        "requires_new_human_approval": True,
        "automatic_redispatch_performed": False,
    }
    child_hash = _recovery_checkpoint_hash(child)
    child["checkpoint_hash"] = child_hash
    child["checkpoint_id"] = f"turtlebot3_recovery_checkpoint_{child_hash[:12]}"
    return child, child_scenario, child_planner


def _build_recovery_failure_followup_checkpoint(
    *,
    parent_checkpoint: Mapping[str, Any],
    proposal: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    segment_results: list[dict[str, Any]],
    approved_recovery_results: list[dict[str, Any]],
    route_failure_observation_results: list[dict[str, Any]],
    recovery_closed_loop_cycles: list[dict[str, Any]],
    autonomy_envelope: Mapping[str, Any],
    battery_envelope: Mapping[str, Any],
    failure_source: str = "approved_recovery_bounded_action_reobservation",
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    dict[str, Any],
]:
    """Re-plan from a failed approved action without reusing its authority."""

    action_result = approved_recovery_results[-1]
    failure_reasons = list(
        parent_checkpoint.get("failure_reasons")
        or action_result.get("blocking_reasons")
        or ["turtlebot3_recovery_goal_not_completed"]
    )
    failure_context = {
        "schema_version": "missionos_turtlebot3_approved_recovery_failure.v1",
        "runtime_failure_observed": True,
        "failed_recovery_checkpoint_id": parent_checkpoint.get("checkpoint_id"),
        "failed_recovery_action": parent_checkpoint.get("selected_action"),
        "failed_recovery_blocking_reasons": failure_reasons,
        "failed_recovery_blocking_reason_count": len(failure_reasons),
        "failed_recovery_result_count": len(approved_recovery_results),
        "failed_recovery_completion_claimed": False,
        "source": failure_source,
        "runtime_failure_source": failure_source,
        "recommended_recovery_action": "ask_human",
        "requires_new_human_approval": True,
    }
    completed_segment_index = int(
        parent_checkpoint.get("next_segment_index") or 1
    ) - 1
    motion_context = _runtime_motion_context(
        action_result=action_result,
        goals=goals,
        segment_index=max(completed_segment_index, 1),
        completed_segment_count=sum(
            1 for item in segment_results if item.get("completion_claimed") is True
        ),
    )
    obstacle_scenario = _runtime_recovery_obstacle_scenario(
        proposal.get("obstacle_scenario")
        if isinstance(proposal.get("obstacle_scenario"), Mapping)
        else {},
        segment_result=action_result,
    )
    reference_goal = goals[max(min(completed_segment_index, len(goals) - 1), 0)]
    home_distance_envelope = _build_home_distance_envelope("", reference_goal)
    home_distance_envelope["distance_to_home_source"] = (
        "approved_recovery_reobservation_projection"
    )
    home_distance_envelope["runtime_observed"] = False
    followup_perception_claims = build_perception_claims_from_env_or_responses(
        tuple(action_result.get("bridge_responses") or ()),
        costmap_obstacle_observed=obstacle_scenario.get(
            "costmap_obstacle_observed"
        )
        is True,
        observed_at=None,
    )
    obstacle_scenario["perception_claims"] = [
        claim.model_dump(mode="json") for claim in followup_perception_claims
    ]
    proposal_models, planner_result = _build_recovery_proposals(
        proposal_id=str(
            proposal.get("proposal_id") or "turtlebot3_home_mission"
        ),
        operator_instruction=str(proposal.get("operator_instruction") or ""),
        battery_envelope=_runtime_recovery_battery_envelope(battery_envelope),
        home_distance_envelope=home_distance_envelope,
        autonomy_envelope=autonomy_envelope,
        obstacle_scenario=obstacle_scenario,
        indoor_delivery_route=proposal.get("indoor_delivery_route")
        if isinstance(proposal.get("indoor_delivery_route"), Mapping)
        else {},
        runtime_failure_context=failure_context,
        runtime_motion_context=motion_context,
        runtime_observation_phase=True,
        harness_stop_dispatcher=lambda reflex: _dispatch_harness_stop(
            reflex=reflex,
            proposal=proposal,
        ),
        perception_claims=followup_perception_claims,
    )
    recovery_proposals = tuple(
        item.model_dump(mode="json") for item in proposal_models
    )
    planner = dict(planner_result)
    classifications = _classify_recovery_proposals(
        autonomy_envelope=autonomy_envelope,
        recovery_proposals=recovery_proposals,
    )
    selected_action = (
        str(recovery_proposals[0].get("selected_action"))
        if recovery_proposals
        else "ask_human"
    )
    repair = None
    if selected_action == "avoid_obstacle":
        repair = _build_recovery_repair_child_checkpoint(
            parent_checkpoint=parent_checkpoint,
            proposal=proposal,
            goals=goals,
            segment_results=segment_results,
            candidate_observation_results=[
                *segment_results,
                *approved_recovery_results,
            ],
            recovery_proposals=recovery_proposals,
            recovery_proposal_classifications=classifications,
            recovery_planner_result=planner,
            obstacle_scenario=obstacle_scenario,
            motion_context=motion_context,
        )
    if repair is not None:
        child, obstacle_scenario, planner = repair
    else:
        if selected_action == "avoid_obstacle":
            original_proposal = (
                dict(recovery_proposals[0]) if recovery_proposals else {}
            )
            constraint_material = {
                "parent_checkpoint_id": parent_checkpoint.get("checkpoint_id"),
                "original_proposal_id": original_proposal.get("proposal_id"),
                "failure_reasons": failure_reasons,
                "decision": "ask_human",
            }
            constraint_hash = hashlib.sha256(
                json.dumps(
                    constraint_material,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            constrained_proposal = {
                **original_proposal,
                "proposal_id": (
                    "mission_autonomy_recovery_proposal_"
                    f"{constraint_hash[:12]}"
                ),
                "proposal_source": (
                    "rules_constrained_after_candidate_resolution_failure"
                ),
                "selected_action": "ask_human",
                "reason": (
                    "The fresh planner proposal could not be bound to a new "
                    "dual-costmap-validated candidate after the approved recovery "
                    "failed. Request bounded operator guidance; do not redispatch "
                    "automatically."
                ),
                "input_observations": {
                    **dict(original_proposal.get("input_observations") or {}),
                    **failure_context,
                    "candidate_resolution_status": "not_validated",
                },
                "original_selected_action": "avoid_obstacle",
                "approval_created": False,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
                "progress_counted": False,
            }
            recovery_proposals = (constrained_proposal, *recovery_proposals)
            classifications = _classify_recovery_proposals(
                autonomy_envelope=autonomy_envelope,
                recovery_proposals=recovery_proposals,
            )
            selected_action = "ask_human"
            planner = {
                **planner,
                "planner_status": (
                    "candidate_resolution_requires_operator_guidance"
                ),
                "execution_proposal": dict(constrained_proposal),
                "original_planner_proposal": original_proposal,
                "automatic_redispatch_performed": False,
            }
            obstacle_scenario = {
                key: value
                for key, value in obstacle_scenario.items()
                if key != "recovery_candidate_resolution"
            }
        child = _build_turtlebot3_recovery_checkpoint(
            proposal=proposal,
            goals=goals,
            segment_results=segment_results,
            recovery_proposals=recovery_proposals,
            recovery_proposal_classifications=classifications,
            recovery_planner_result=planner,
            runtime_recovery_obstacle_scenario=obstacle_scenario,
            runtime_recovery_motion_context=motion_context,
            completed_segment_index=completed_segment_index,
            route_failure_observation_results=route_failure_observation_results,
        )
        child = {
            **child,
            "parent_checkpoint_id": parent_checkpoint.get("checkpoint_id"),
            "parent_checkpoint_hash": parent_checkpoint.get("checkpoint_hash"),
            "repair_attempt": int(parent_checkpoint.get("repair_attempt") or 0) + 1,
            "repair_trigger": "approved_recovery_reobservation_failed",
            "operator_guidance_required": selected_action
            in {"ask_human", "hold", "safe_stop"},
            "requires_new_human_approval": True,
            "automatic_redispatch_performed": False,
        }
    child = {
        **child,
        "prior_closed_loop_cycle_refs": [
            {
                "cycle_index": cycle.get("cycle_index"),
                "checkpoint_id": cycle.get("checkpoint_id"),
                "reobservation_sha256": cycle.get("reobservation_sha256"),
            }
            for cycle in recovery_closed_loop_cycles
        ],
        "requires_new_human_approval": True,
        "automatic_redispatch_performed": False,
    }
    child_hash = _recovery_checkpoint_hash(child)
    child["checkpoint_hash"] = child_hash
    child["checkpoint_id"] = f"turtlebot3_recovery_checkpoint_{child_hash[:12]}"
    return (
        child,
        obstacle_scenario,
        planner,
        recovery_proposals,
        classifications,
        motion_context,
    )


def _recovery_checkpoint_from_execution(
    resume_execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(resume_execution, Mapping):
        return {}
    if resume_execution.get("schema_version") == TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA:
        return dict(resume_execution)
    checkpoint = resume_execution.get("turtlebot3_recovery_checkpoint")
    if isinstance(checkpoint, Mapping):
        return dict(checkpoint)
    execution = resume_execution.get("turtlebot3_home_mission_execution")
    if isinstance(execution, Mapping):
        checkpoint = execution.get("turtlebot3_recovery_checkpoint")
        if isinstance(checkpoint, Mapping):
            return dict(checkpoint)
    summary = resume_execution.get("summary")
    if isinstance(summary, Mapping):
        checkpoint = summary.get("turtlebot3_recovery_checkpoint")
        if isinstance(checkpoint, Mapping):
            return dict(checkpoint)
    return {}


def _recovery_resume_payload(
    resume_execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(resume_execution, Mapping):
        return {}
    execution = resume_execution.get("turtlebot3_home_mission_execution")
    if isinstance(execution, Mapping):
        return dict(execution)
    return dict(resume_execution)


def _turtlebot3_recovery_revision_intent(
    operator_instruction: str,
) -> tuple[str | None, list[str]]:
    """Resolve a bounded operator constraint without treating text as authority."""

    import re

    text = str(operator_instruction or "").strip().lower()
    compact = text.replace(" ", "").replace("　", "")
    normalized_apostrophes = text.replace("’", "'")
    # This parser sits before an authority-bearing recovery proposal. Treat it
    # as a conservative command allowlist, not a general intent classifier:
    # universal rejection tokens fail the whole instruction closed, and a
    # positive action is accepted only when the full utterance ends in one of
    # the supported imperative/request shapes below.
    japanese_global_rejection = any(
        token in compact
        for token in (
            "無し",
            "なし",
            "不可",
            "不採用",
            "拒否",
            "却下",
            "禁止",
            "中止",
            "不要",
            "ダメ",
            "駄目",
            "やめ",
        )
    ) or re.search(r"(?<![a-z0-9])ng(?![a-z0-9])", compact)
    english_global_rejection = re.search(
        r"\b(?:absolutely\s+not|no\s+way|forbidden|prohibited|"
        r"reject(?:ed|ing)?|cancel(?:ed|ing|led|ling)?|never)\b",
        normalized_apostrophes,
    )
    if japanese_global_rejection or english_global_rejection:
        return None, ["operator_recovery_revision_negated_intent_not_executable"]
    japanese_negated_direction = re.search(
        r"(?:左|右)(?:側|方向)?.{0,16}"
        r"(?:ない|なく|ません|ぬ|ず|禁止|却下|やめ|以外|除い|抜き|なし)",
        text,
    )
    japanese_avoided_direction = re.search(
        r"(?:左|右)(?:側|方向)?(?:を|に|へ)?"
        r"(?:通る|通って|進む|行く|曲がる|回る)"
        r".{0,8}(?:避け|やめ|危険)",
        text,
    )
    japanese_rejected_direction = re.search(
        r"(?:左|右)(?:側|方向)?.{0,28}"
        r"(?:不採用|拒否|却下|ダメ|駄目|中止)",
        text,
    )
    japanese_dangerous_direction = re.search(
        r"(?:左|右)(?:側|方向)?.{0,18}"
        r"(?:曲が|旋回|回|迂回|進|行|通|避け|かわ).{0,10}危険",
        text,
    )
    japanese_negated_return = re.search(
        r"(?:引き返|帰還|ホーム.{0,8}戻|出発(?:地点|点)?.{0,8}戻)"
        r".{0,12}(?:ない|ません|ぬ|ず|不要|禁止|却下|危険|避け|やめ)",
        text,
    )
    english_negator = (
        r"(?:do\s+not|don't|dont|never|must\s+not|should\s+not|shouldn't|"
        r"cannot|can't)"
    )
    english_negated_direction_or_return = re.search(
        english_negator
        + r".{0,40}(?:left|right|return\s+home|return\s+to\s+home|"
        r"turn\s+back|go\s+back|head\s+back|retreat)",
        normalized_apostrophes,
    )
    english_excluded_direction = re.search(
        r"(?:\bwithout\b.{0,40}|\b(?:except|excluding|other\s+than|"
        r"anything\s+but|not|no)\s+(?:the\s+)?)\b(?:left|right)\b",
        normalized_apostrophes,
    )
    english_avoided_return = re.search(
        r"\bavoid(?:ing)?\b.{0,24}(?:return\s+home|return\s+to\s+home|"
        r"turn\s+back|go\s+back|head\s+back|retreat)",
        normalized_apostrophes,
    )
    english_excluded_return = re.search(
        r"\b(?:anything\s+but|except|excluding|other\s+than)\s+"
        r"(?:return\s+home|return\s+to\s+home|turn\s+back|go\s+back|"
        r"head\s+back|retreat)\b",
        normalized_apostrophes,
    )
    english_rejected_direction_or_return = re.search(
        r"\b(?:left|right|return\s+home|return\s+to\s+home|turn\s+back|"
        r"go\s+back|head\s+back|retreat)\b.{0,28}"
        r"(?:actually\s+no|(?:is\s+)?(?:forbidden|prohibited|dangerous|unsafe|"
        r"rejected|cancelled|canceled)|is\s+not\s+(?:needed|required|allowed))",
        normalized_apostrophes,
    )
    english_rejected_direction_prefix = re.search(
        r"\b(?:reject|cancel)(?:ed|ing|led|ling)?\b.{0,24}"
        r"\b(?:turn(?:ing)?|go(?:ing)?|move|moving|head|heading|veer|veering)?"
        r"\s*(?:left|right)\b",
        normalized_apostrophes,
    )
    if (
        japanese_negated_direction
        or japanese_avoided_direction
        or japanese_rejected_direction
        or japanese_dangerous_direction
        or japanese_negated_return
        or english_negated_direction_or_return
        or english_excluded_direction
        or english_avoided_return
        or english_excluded_return
        or english_rejected_direction_or_return
        or english_rejected_direction_prefix
    ):
        return None, ["operator_recovery_revision_negated_intent_not_executable"]
    altitude_requested = any(
        term in text
        for term in (
            "高度",
            "上昇",
            "上空",
            "飛び越",
            "climb",
            "altitude",
            "higher",
            "above",
            "go over",
            "over it",
        )
    ) or any(term in compact for term in ("上を通", "上から越"))
    japanese_motion_modifier = (
        r"(?:(?:大きく|広く|大回りで?|ゆっくり|少し|一旦|急に|しっかり)){0,3}"
    )
    japanese_directed_command = (
        r"(?:曲が(?:って|れ)|旋回(?:して|しろ|せよ)|"
        r"回避(?:して|しろ|せよ)|回(?:って|れ)|"
        r"迂回(?:して|しろ|せよ)|進(?:んで|め)|"
        r"行(?:って|け)|通(?:って|れ)|避け(?:て|ろ)|"
        r"かわ(?:して|せ))"
    )
    japanese_path_command = (
        r"(?:曲が(?:って|れ)|旋回(?:して|しろ|せよ)|"
        r"回(?:って|れ)|迂回(?:して|しろ|せよ)|"
        r"進(?:んで|め)|通(?:って|れ)|かわ(?:して|せ))"
    )
    japanese_command_continuation = (
        r"(?:(?:(?:障害物|それ|これ)を)?"
        r"(?:避けて|かわして|通って|進んで))*"
    )
    japanese_polite_suffix = (
        r"(?:ください|下さい|くれ|ほしい|欲しい|"
        r"お願い(?:します)?)?"
    )
    japanese_command_text = compact.rstrip("。！!")
    english_command_text = normalized_apostrophes.strip().rstrip(".!").strip()

    def _japanese_positive_side_request(side: str) -> bool:
        return bool(
            re.fullmatch(
                rf".*?{side}(?:側|方向)?(?:に|へ|から)"
                rf"{japanese_motion_modifier}{japanese_directed_command}"
                rf"{japanese_command_continuation}{japanese_polite_suffix}",
                japanese_command_text,
            )
            or re.fullmatch(
                rf".*?{side}(?:側)?を{japanese_motion_modifier}"
                rf"{japanese_path_command}{japanese_command_continuation}"
                rf"{japanese_polite_suffix}",
                japanese_command_text,
            )
        )

    def _english_positive_side_request(side: str) -> bool:
        direct_motion = re.fullmatch(
            rf"(?:please\s+)?(?:turn|veer|move|go|head|bear|keep|detour)"
            rf"(?:\s+(?:sharply|widely|wide|hard|slightly))?\s+"
            rf"(?:to\s+(?:the\s+)?)?{side}(?:\s+please)?",
            english_command_text,
        )
        routed_motion = bool(
            re.fullmatch(
                rf"(?:please\s+)?take\b.{{0,24}}\bturn\b.{{0,32}}"
                rf"\b(?:on|via)\s+(?:the\s+)?{side}(?:\s+side)?"
                rf"(?:\s+please)?",
                english_command_text,
            )
            or re.fullmatch(
                rf"(?:please\s+)?(?:go|move|head|proceed|travel|detour|pass|navigate)"
                rf"\b.{{0,24}}\b(?:around|past)\b.{{0,24}}\bvia\s+"
                rf"(?:the\s+)?{side}(?:\s+side)?(?:\s+please)?",
                english_command_text,
            )
        )
        make_turn = re.fullmatch(
            rf"(?:please\s+)?(?:make|take)\s+(?:a\s+)?(?:wide\s+)?"
            rf"{side}\s+turn(?:\s+please)?",
            english_command_text,
        )
        return bool(direct_motion or routed_motion or make_turn)

    left_requested = _japanese_positive_side_request(
        "左"
    ) or _english_positive_side_request("left")
    right_requested = _japanese_positive_side_request(
        "右"
    ) or _english_positive_side_request("right")
    japanese_return_then_resume_requested = re.fullmatch(
        rf".*?(?:引き返(?:して|せ)|帰還(?:して|しろ|せよ)|"
        rf"(?:出発地点|出発点|ホーム|家)(?:へ|に)?(?:一旦)?戻(?:って|れ))"
        rf"(?:から|後(?:に)?|、|,)?(?:配送|走行|ルート|ミッション)(?:を)?"
        rf"(?:再開(?:して|しろ|せよ)|続け(?:て|ろ))"
        rf"{japanese_polite_suffix}",
        japanese_command_text,
    )
    english_return_then_resume_requested = re.fullmatch(
        r"(?:please\s+)?(?:return\s+home|return\s+to\s+home|turn\s+back|"
        r"go\s+back|head\s+back|retreat)(?:,|\s+and|\s+then)*\s+"
        r"(?:resume|continue)(?:\s+the)?\s+(?:delivery|route|mission)"
        r"(?:\s+please)?",
        english_command_text,
    )
    return_then_resume_requested = bool(
        japanese_return_then_resume_requested
        or english_return_then_resume_requested
    )
    japanese_return_requested = re.fullmatch(
        rf".*?(?:引き返(?:して|せ)|帰還(?:して|しろ|せよ)|"
        rf"(?:出発地点|出発点|ホーム|家)(?:へ|に)?戻(?:って|れ))"
        rf"{japanese_polite_suffix}",
        japanese_command_text,
    )
    english_return_requested = re.fullmatch(
        r"(?:please\s+)?(?:return\s+home|return\s+to\s+home|turn\s+back|"
        r"go\s+back|head\s+back|retreat)(?:\s+(?:now|please))?",
        english_command_text,
    )
    return_requested = bool(japanese_return_requested or english_return_requested)
    retry_failed_segment_requested = bool(
        re.fullmatch(
            rf".*?(?:停止|失敗)(?:を)?確認(?:した|して)?[、,。 ]*"
            rf"(?:同じ|直前の)?(?:配送)?(?:区間|ルート|セグメント)(?:を)?"
            rf"(?:一度だけ)?再試行(?:して|しろ|せよ){japanese_polite_suffix}",
            japanese_command_text,
        )
        or re.fullmatch(
            r"(?:please\s+)?retry\s+(?:the\s+)?(?:failed|same|last)\s+"
            r"(?:route\s+)?segment\s+once(?:\s+please)?",
            english_command_text,
        )
    )
    if altitude_requested:
        return None, ["operator_recovery_revision_unsupported_for_ground_robot"]
    if left_requested and right_requested:
        return None, ["operator_recovery_revision_direction_ambiguous"]
    if (return_requested or return_then_resume_requested) and (
        left_requested or right_requested
    ):
        return None, ["operator_recovery_revision_action_ambiguous"]
    if return_then_resume_requested:
        return "return_home_then_resume", []
    if retry_failed_segment_requested:
        return "retry_failed_segment", []
    if return_requested:
        return "return_home", []
    if left_requested:
        return "avoid_left_wide", []
    if right_requested:
        return "avoid_right_wide", []
    return None, ["operator_recovery_revision_intent_not_supported"]


def _revision_numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _revision_point_to_segment_distance_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    ratio = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / length_squared,
        ),
    )
    closest = (start[0] + ratio * dx, start[1] + ratio * dy)
    return math.hypot(point[0] - closest[0], point[1] - closest[1])


def _revision_point_in_polygon(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
) -> bool:
    if len(polygon) < 3:
        return False
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            x_crossing = (previous[0] - current[0]) * (
                point[1] - current[1]
            ) / (previous[1] - current[1]) + current[0]
            if point[0] < x_crossing:
                inside = not inside
        previous = current
    return inside


def _revision_oriented_rect_clearance_m(
    point: tuple[float, float],
    record: Mapping[str, Any],
) -> float | None:
    center_x = _revision_numeric(record.get("x_m"))
    center_y = _revision_numeric(record.get("y_m"))
    size_x = _revision_numeric(record.get("size_x_m"))
    size_y = _revision_numeric(record.get("size_y_m"))
    yaw = _revision_numeric(record.get("yaw_rad")) or 0.0
    if (
        center_x is None
        or center_y is None
        or size_x is None
        or size_y is None
        or size_x <= 0.0
        or size_y <= 0.0
    ):
        return None
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    relative_x = point[0] - center_x
    relative_y = point[1] - center_y
    local = (
        cos_yaw * relative_x + sin_yaw * relative_y,
        -sin_yaw * relative_x + cos_yaw * relative_y,
    )
    return _point_rect_clearance_m(
        local,
        (-size_x / 2.0, size_x / 2.0, -size_y / 2.0, size_y / 2.0),
    )


def _revision_floor_plan_waypoint_clearance(
    point: tuple[float, float],
    floor_plan: Mapping[str, Any],
) -> tuple[float | None, list[str]]:
    reasons: list[str] = []
    clearances: list[float] = []
    bounds = floor_plan.get("bounds")
    bounds = bounds if isinstance(bounds, Mapping) else {}
    min_x = _revision_numeric(bounds.get("min_x_m"))
    max_x = _revision_numeric(bounds.get("max_x_m"))
    min_y = _revision_numeric(bounds.get("min_y_m"))
    max_y = _revision_numeric(bounds.get("max_y_m"))
    if None not in (min_x, max_x, min_y, max_y):
        boundary_clearance = min(
            point[0] - float(min_x),
            float(max_x) - point[0],
            point[1] - float(min_y),
            float(max_y) - point[1],
        )
        clearances.append(boundary_clearance)
        if boundary_clearance < _TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M:
            reasons.append("operator_recovery_revision_waypoint_outside_floor_bounds")

    polygon = [
        (float(x), float(y))
        for item in floor_plan.get("wall_polygon") or ()
        if isinstance(item, Mapping)
        and (x := _revision_numeric(item.get("x_m"))) is not None
        and (y := _revision_numeric(item.get("y_m"))) is not None
    ]
    if polygon:
        if not _revision_point_in_polygon(point, polygon):
            reasons.append("operator_recovery_revision_waypoint_outside_wall_polygon")
        else:
            polygon_clearance = min(
                _revision_point_to_segment_distance_m(point, start, end)
                for start, end in zip(polygon, [*polygon[1:], polygon[0]], strict=True)
            )
            clearances.append(polygon_clearance)
            if (
                polygon_clearance
                < _TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M
            ):
                reasons.append(
                    "operator_recovery_revision_waypoint_wall_clearance_insufficient"
                )

    for collection_name in ("walls", "furniture"):
        for record in floor_plan.get(collection_name) or ():
            if not isinstance(record, Mapping):
                continue
            clearance = _revision_oriented_rect_clearance_m(point, record)
            if clearance is None:
                continue
            clearances.append(clearance)
            if (
                clearance
                < _TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M
            ):
                reasons.append(
                    "operator_recovery_revision_waypoint_static_collision_clearance_insufficient"
                )

    for record in floor_plan.get("pillars") or ():
        if not isinstance(record, Mapping):
            continue
        center_x = _revision_numeric(record.get("x_m"))
        center_y = _revision_numeric(record.get("y_m"))
        radius = _revision_numeric(record.get("radius_m"))
        if center_x is None or center_y is None or radius is None:
            continue
        clearance = math.hypot(point[0] - center_x, point[1] - center_y) - radius
        clearances.append(clearance)
        if clearance < _TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M:
            reasons.append(
                "operator_recovery_revision_waypoint_static_collision_clearance_insufficient"
            )
    return (min(clearances) if clearances else None), list(dict.fromkeys(reasons))


def _revision_floor_plan_from_execution(
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    map_model = execution.get("turtlebot3_indoor_map_model")
    map_model = map_model if isinstance(map_model, Mapping) else {}
    floor_plan = map_model.get("floor_plan")
    return dict(floor_plan) if isinstance(floor_plan, Mapping) else {}


def _revision_reobserved_anchor_pose(
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the latest source-backed pose observed after a recovery action."""

    for collection_name in (
        "approved_recovery_segment_results",
        "recovery_attempt_history",
    ):
        results = execution.get(collection_name)
        if not isinstance(results, list):
            continue
        for result in reversed(results):
            if not isinstance(result, Mapping):
                continue
            raw_points, _filter = _raw_map_observed_trajectory_points(
                _observed_points_from_action_result(result)
            )
            if not raw_points:
                continue
            latest = raw_points[-1]
            x_m = _revision_numeric(latest.get("x_m"))
            y_m = _revision_numeric(latest.get("y_m"))
            if x_m is None or y_m is None:
                continue
            return {
                "x_m": x_m,
                "y_m": y_m,
                "source": "latest_approved_recovery_raw_map_frame_observation",
                "segment_ref": result.get("segment_ref"),
                "sample_index": latest.get("sample_index"),
                "frame_id": "map",
                "completion_claimed": result.get("completion_claimed") is True,
            }
    return {}


def _revision_floor_plan_geometry_sha256(
    floor_plan: Mapping[str, Any],
) -> str:
    if not floor_plan:
        return ""
    source_geometry = {
        "floor_plan_id": floor_plan.get("floor_plan_id"),
        "source": floor_plan.get("source"),
        "bounds": floor_plan.get("bounds") or {},
        "wall_polygon": floor_plan.get("wall_polygon") or [],
        "walls": floor_plan.get("walls") or [],
        "furniture": floor_plan.get("furniture") or [],
        "pillars": floor_plan.get("pillars") or [],
    }
    return hashlib.sha256(
        json.dumps(
            source_geometry,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _revision_source_obstacle(
    execution: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    scenario = execution.get("runtime_recovery_obstacle_scenario")
    scenario = scenario if isinstance(scenario, Mapping) else {}
    map_model = execution.get("turtlebot3_indoor_map_model")
    map_model = map_model if isinstance(map_model, Mapping) else {}
    x_m = _revision_numeric(scenario.get("runtime_obstacle_x_m"))
    y_m = _revision_numeric(scenario.get("runtime_obstacle_y_m"))
    size_x_m = _revision_numeric(scenario.get("runtime_obstacle_size_x_m"))
    size_y_m = _revision_numeric(scenario.get("runtime_obstacle_size_y_m"))
    scene_ref = str(scenario.get("runtime_obstacle_scene_ref") or "")
    for marker in map_model.get("obstacles") or ():
        if not isinstance(marker, Mapping):
            continue
        marker_x = _revision_numeric(marker.get("x_m"))
        marker_y = _revision_numeric(marker.get("y_m"))
        marker_name = str(marker.get("name") or "")
        matches = bool(scene_ref and marker_name == scene_ref) or (
            x_m is not None
            and y_m is not None
            and marker_x is not None
            and marker_y is not None
            and math.isclose(marker_x, x_m, abs_tol=1e-6)
            and math.isclose(marker_y, y_m, abs_tol=1e-6)
        )
        if not matches:
            continue
        size_x_m = size_x_m or _revision_numeric(marker.get("size_x_m"))
        size_y_m = size_y_m or _revision_numeric(marker.get("size_y_m"))
        scene_ref = scene_ref or marker_name
        break
    reasons: list[str] = []
    if scenario.get("runtime_obstacle_observed") is not True:
        reasons.append("operator_recovery_revision_source_obstacle_not_observed")
    if None in (x_m, y_m, size_x_m, size_y_m) or float(size_x_m or 0.0) <= 0.0 or float(
        size_y_m or 0.0
    ) <= 0.0:
        reasons.append("operator_recovery_revision_source_obstacle_geometry_missing")
    if reasons:
        return {}, reasons
    return {
        "x_m": float(x_m),
        "y_m": float(y_m),
        "size_x_m": float(size_x_m),
        "size_y_m": float(size_y_m),
        "scene_ref": scene_ref,
        "observation_source": scenario.get("runtime_obstacle_source"),
        "geometry_source": scenario.get("runtime_obstacle_geometry_source")
        or "stored_turtlebot3_indoor_map_obstacle",
    }, []


def _turtlebot3_recovery_revision_source_geometry_reasons(
    *,
    checkpoint: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> list[str]:
    """Revalidate revision source geometry before supersede or dispatch."""

    if not str(checkpoint.get("revision_id") or ""):
        return []
    geometry = checkpoint.get("recovery_revision_geometry")
    geometry = geometry if isinstance(geometry, Mapping) else {}
    selected_action = str(checkpoint.get("selected_action") or "")
    reasons: list[str] = []
    if not geometry:
        return ["turtlebot3_recovery_revision_geometry_missing"]
    if selected_action == "avoid_obstacle":
        floor_plan = _revision_floor_plan_from_execution(execution)
        current_floor_hash = _revision_floor_plan_geometry_sha256(floor_plan)
        if (
            not current_floor_hash
            or str(geometry.get("floor_plan_geometry_sha256") or "")
            != current_floor_hash
            or str(geometry.get("floor_plan_id") or "")
            != str(floor_plan.get("floor_plan_id") or "")
            or str(geometry.get("floor_plan_geometry_source") or "")
            != str(floor_plan.get("source") or "")
        ):
            reasons.append("turtlebot3_recovery_revision_floor_plan_geometry_changed")
        source_obstacle, obstacle_reasons = _revision_source_obstacle(execution)
        reasons.extend(obstacle_reasons)
        bound_obstacle = geometry.get("obstacle")
        bound_obstacle = (
            bound_obstacle if isinstance(bound_obstacle, Mapping) else {}
        )
        for key in ("x_m", "y_m", "size_x_m", "size_y_m"):
            if _revision_numeric(bound_obstacle.get(key)) != _revision_numeric(
                source_obstacle.get(key)
            ):
                reasons.append(
                    "turtlebot3_recovery_revision_obstacle_geometry_changed"
                )
                break
        if str(bound_obstacle.get("scene_ref") or "") != str(
            source_obstacle.get("scene_ref") or ""
        ):
            reasons.append("turtlebot3_recovery_revision_obstacle_geometry_changed")
    elif selected_action == "return_home":
        map_model = execution.get("turtlebot3_indoor_map_model")
        map_model = map_model if isinstance(map_model, Mapping) else {}
        current_home = next(
            (
                item
                for item in map_model.get("planned_points") or ()
                if isinstance(item, Mapping) and item.get("role") == "home"
            ),
            {},
        )
        goal_poses = checkpoint.get("recovery_goal_poses")
        goal_poses = goal_poses if isinstance(goal_poses, list) else []
        bound_home = (
            goal_poses[0]
            if len(goal_poses) == 1 and isinstance(goal_poses[0], Mapping)
            else {}
        )
        if (
            _revision_numeric(current_home.get("x_m"))
            != _revision_numeric(bound_home.get("x_m"))
            or _revision_numeric(current_home.get("y_m"))
            != _revision_numeric(bound_home.get("y_m"))
        ):
            reasons.append("turtlebot3_recovery_revision_home_pose_changed")
    elif selected_action == "reroute":
        failures = execution.get("route_failure_observation_results")
        failures = failures if isinstance(failures, list) else []
        failed = (
            failures[-1]
            if failures and isinstance(failures[-1], Mapping)
            else {}
        )
        failure_sha256 = (
            hashlib.sha256(
                json.dumps(
                    failed,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if failed
            else ""
        )
        if (
            not failure_sha256
            or geometry.get("failed_segment_observation_sha256")
            != failure_sha256
            or geometry.get("failed_segment_ref") != failed.get("segment_ref")
        ):
            reasons.append(
                "turtlebot3_recovery_revision_failed_segment_observation_changed"
            )
        segment_ref = str(failed.get("segment_ref") or "")
        try:
            segment_index = int(segment_ref.removeprefix("segment_"))
        except ValueError:
            segment_index = 0
        planned_segments = execution.get("planned_segments")
        planned_segments = (
            planned_segments if isinstance(planned_segments, list) else []
        )
        expected_goal = (
            planned_segments[segment_index - 1]
            if 0 < segment_index <= len(planned_segments)
            and isinstance(planned_segments[segment_index - 1], Mapping)
            else {}
        )
        goal_poses = checkpoint.get("recovery_goal_poses")
        goal_poses = goal_poses if isinstance(goal_poses, list) else []
        bound_goal = (
            goal_poses[0]
            if len(goal_poses) == 1 and isinstance(goal_poses[0], Mapping)
            else {}
        )
        next_route_goal = geometry.get("next_route_goal")
        next_route_goal = (
            next_route_goal if isinstance(next_route_goal, Mapping) else {}
        )
        if (
            not expected_goal
            or next_route_goal.get("segment_index") != segment_index
            or _revision_numeric(next_route_goal.get("x_m"))
            != _revision_numeric(expected_goal.get("x_m"))
            or _revision_numeric(next_route_goal.get("y_m"))
            != _revision_numeric(expected_goal.get("y_m"))
            or _revision_numeric(bound_goal.get("x_m"))
            != _revision_numeric(expected_goal.get("x_m"))
            or _revision_numeric(bound_goal.get("y_m"))
            != _revision_numeric(expected_goal.get("y_m"))
        ):
            reasons.append(
                "turtlebot3_recovery_revision_failed_segment_goal_changed"
            )
    else:
        reasons.append("turtlebot3_recovery_revision_action_not_supported")
    return list(dict.fromkeys(reasons))


def _revision_goal_payload(
    *,
    template: Mapping[str, Any],
    x_m: float,
    y_m: float,
    label: str,
) -> dict[str, Any]:
    return Nav2GoalPose.model_validate(
        {
            **dict(template),
            "frame_id": "map",
            "x_m": round(x_m, 6),
            "y_m": round(y_m, 6),
            "yaw_rad": 0.0,
            "label": label,
        }
    ).model_dump(mode="json")


def _build_directional_recovery_revision_geometry(
    *,
    direction: Literal["left", "right"],
    proposal: Mapping[str, Any],
    execution: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    segment_results = execution.get("segment_results")
    segment_results = segment_results if isinstance(segment_results, list) else []
    if not segment_results or not isinstance(segment_results[-1], Mapping):
        return {}, ["operator_recovery_revision_completed_segment_source_missing"]
    completed_result = segment_results[-1]
    if completed_result.get("completion_claimed") is not True:
        return {}, ["operator_recovery_revision_completed_segment_not_verified"]
    anchor_goal = completed_result.get("goal_pose")
    anchor_goal = anchor_goal if isinstance(anchor_goal, Mapping) else {}
    route_anchor_x = _revision_numeric(anchor_goal.get("x_m"))
    route_anchor_y = _revision_numeric(anchor_goal.get("y_m"))
    next_index = checkpoint.get("next_segment_index")
    planned_segments = proposal.get("planned_segments")
    planned_segments = planned_segments if isinstance(planned_segments, (list, tuple)) else ()
    if (
        route_anchor_x is None
        or route_anchor_y is None
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 1
        or next_index > len(planned_segments)
        or not isinstance(planned_segments[next_index - 1], Mapping)
    ):
        return {}, ["operator_recovery_revision_route_geometry_source_missing"]
    next_goal = planned_segments[next_index - 1]
    next_x = _revision_numeric(next_goal.get("x_m"))
    next_y = _revision_numeric(next_goal.get("y_m"))
    if next_x is None or next_y is None:
        return {}, ["operator_recovery_revision_route_geometry_source_missing"]
    route_dx = next_x - route_anchor_x
    route_dy = next_y - route_anchor_y
    route_length = math.hypot(route_dx, route_dy)
    if route_length <= 1e-6:
        return {}, ["operator_recovery_revision_route_direction_degenerate"]
    direction_unit = (route_dx / route_length, route_dy / route_length)
    left_unit = (-direction_unit[1], direction_unit[0])
    side_sign = 1.0 if direction == "left" else -1.0
    reobserved_anchor = _revision_reobserved_anchor_pose(execution)
    anchor_x = _revision_numeric(reobserved_anchor.get("x_m"))
    anchor_y = _revision_numeric(reobserved_anchor.get("y_m"))
    if anchor_x is None or anchor_y is None:
        anchor_x = route_anchor_x
        anchor_y = route_anchor_y
        reobserved_anchor = {
            "x_m": anchor_x,
            "y_m": anchor_y,
            "source": "last_completed_nav2_segment_goal",
            "segment_ref": completed_result.get("segment_ref"),
            "completion_claimed": True,
            "frame_id": "map",
        }
    obstacle, obstacle_reasons = _revision_source_obstacle(execution)
    if obstacle_reasons:
        return {}, obstacle_reasons
    obstacle_from_anchor = (
        obstacle["x_m"] - route_anchor_x,
        obstacle["y_m"] - route_anchor_y,
    )
    obstacle_route_projection = (
        obstacle_from_anchor[0] * direction_unit[0]
        + obstacle_from_anchor[1] * direction_unit[1]
    )
    obstacle_route_lateral_offset = abs(
        obstacle_from_anchor[0] * left_unit[0]
        + obstacle_from_anchor[1] * left_unit[1]
    )
    if (
        obstacle_route_projection
        < -_TURTLEBOT3_RECOVERY_REVISION_LONGITUDINAL_BUFFER_M
        or obstacle_route_projection
        > route_length + _TURTLEBOT3_RECOVERY_REVISION_LONGITUDINAL_BUFFER_M
        or obstacle_route_lateral_offset
        > _TURTLEBOT3_RECOVERY_REVISION_MAX_LATERAL_OFFSET_M
    ):
        return {}, ["operator_recovery_revision_obstacle_not_bound_to_next_segment"]
    half_x = obstacle["size_x_m"] / 2.0
    half_y = obstacle["size_y_m"] / 2.0
    parallel_support = abs(direction_unit[0]) * half_x + abs(direction_unit[1]) * half_y
    perpendicular_support = abs(left_unit[0]) * half_x + abs(left_unit[1]) * half_y
    wide_bbox_clearance = _TURTLEBOT3_RECOVERY_REVISION_WIDE_BBOX_CLEARANCE_M
    lateral_offset = perpendicular_support + wide_bbox_clearance
    if lateral_offset > _TURTLEBOT3_RECOVERY_REVISION_MAX_LATERAL_OFFSET_M:
        return {}, ["operator_recovery_revision_lateral_offset_exceeds_bound"]
    longitudinal_offset = (
        parallel_support + _TURTLEBOT3_RECOVERY_REVISION_LONGITUDINAL_BUFFER_M
    )
    obstacle_center = (obstacle["x_m"], obstacle["y_m"])
    if reobserved_anchor.get("source") == (
        "latest_approved_recovery_raw_map_frame_observation"
    ):
        # The previous bounded action changed the robot's pose. Treat that
        # re-observation as the new start and bind a route-rejoin shoulder on
        # the requested side. Reusing the pre-recovery entry/exit pair here
        # can force the robot back across the obstacle or another live cost.
        anchor_from_route = (
            anchor_x - route_anchor_x,
            anchor_y - route_anchor_y,
        )
        anchor_route_projection = (
            anchor_from_route[0] * direction_unit[0]
            + anchor_from_route[1] * direction_unit[1]
        )
        entry_longitudinal = (
            -longitudinal_offset
            if anchor_route_projection <= obstacle_route_projection
            else min(
                max(
                    anchor_route_projection - obstacle_route_projection,
                    longitudinal_offset,
                ),
                route_length - obstacle_route_projection,
            )
        )
        entry = (
            obstacle_center[0]
            + direction_unit[0] * entry_longitudinal
            + side_sign * left_unit[0] * lateral_offset,
            obstacle_center[1]
            + direction_unit[1] * entry_longitudinal
            + side_sign * left_unit[1] * lateral_offset,
        )
        exit_longitudinal = route_length - obstacle_route_projection
        exit_point = (
            obstacle_center[0]
            + direction_unit[0] * exit_longitudinal
            + side_sign * left_unit[0] * lateral_offset,
            obstacle_center[1]
            + direction_unit[1] * exit_longitudinal
            + side_sign * left_unit[1] * lateral_offset,
        )
        geometry_strategy = "reobserved_anchor_via_side_shoulder_to_route_rejoin"
    else:
        entry = (
            obstacle_center[0]
            - direction_unit[0] * longitudinal_offset
            + side_sign * left_unit[0] * lateral_offset,
            obstacle_center[1]
            - direction_unit[1] * longitudinal_offset
            + side_sign * left_unit[1] * lateral_offset,
        )
        exit_point = (
            obstacle_center[0]
            + direction_unit[0] * longitudinal_offset
            + side_sign * left_unit[0] * lateral_offset,
            obstacle_center[1]
            + direction_unit[1] * longitudinal_offset
            + side_sign * left_unit[1] * lateral_offset,
        )
        geometry_strategy = "route_bound_entry_exit"
    floor_plan = _revision_floor_plan_from_execution(execution)
    if not floor_plan:
        return {}, ["operator_recovery_revision_floor_plan_source_missing"]
    floor_plan_geometry_sha256 = _revision_floor_plan_geometry_sha256(floor_plan)
    waypoint_clearances: list[float | None] = []
    blocking_reasons: list[str] = []
    obstacle_rect = (
        obstacle_center[0] - half_x,
        obstacle_center[0] + half_x,
        obstacle_center[1] - half_y,
        obstacle_center[1] + half_y,
    )
    for point in (entry, exit_point):
        obstacle_clearance = _point_rect_clearance_m(point, obstacle_rect)
        if (
            obstacle_clearance + 1e-9
            < wide_bbox_clearance
        ):
            blocking_reasons.append(
                "operator_recovery_revision_obstacle_clearance_insufficient"
            )
        static_clearance, static_reasons = _revision_floor_plan_waypoint_clearance(
            point,
            floor_plan,
        )
        waypoint_clearances.append(static_clearance)
        blocking_reasons.extend(static_reasons)
    detour_distance = (
        math.hypot(entry[0] - anchor_x, entry[1] - anchor_y)
        + math.hypot(exit_point[0] - entry[0], exit_point[1] - entry[1])
        + math.hypot(next_x - exit_point[0], next_y - exit_point[1])
    )
    if detour_distance > _TURTLEBOT3_RECOVERY_REVISION_MAX_DETOUR_DISTANCE_M:
        blocking_reasons.append("operator_recovery_revision_detour_distance_exceeds_bound")
    if blocking_reasons:
        return {}, list(dict.fromkeys(blocking_reasons))
    goal_poses = [
        _revision_goal_payload(
            template=next_goal,
            x_m=entry[0],
            y_m=entry[1],
            label=f"operator_revision_{direction}_wide_avoidance_entry",
        ),
        _revision_goal_payload(
            template=next_goal,
            x_m=exit_point[0],
            y_m=exit_point[1],
            label=f"operator_revision_{direction}_wide_avoidance_exit",
        ),
    ]
    geometry = {
        "schema_version": "missionos_turtlebot3_recovery_revision_geometry.v1",
        "geometry_status": "source_bound_candidate",
        "geometry_strategy": geometry_strategy,
        "direction_reference": "planned_travel_direction_a_to_b_in_map_frame",
        "requested_direction": direction,
        "clearance_profile": "wide",
        "anchor_pose": dict(reobserved_anchor),
        "route_anchor_pose": {
            "x_m": route_anchor_x,
            "y_m": route_anchor_y,
            "source": "last_completed_nav2_segment_goal",
            "segment_ref": completed_result.get("segment_ref"),
            "completion_claimed": True,
        },
        "next_route_goal": {
            "x_m": next_x,
            "y_m": next_y,
            "source": "original_turtlebot3_planned_segment",
            "segment_index": next_index,
            "label": next_goal.get("label"),
        },
        "obstacle": dict(obstacle),
        "floor_plan_id": floor_plan.get("floor_plan_id"),
        "floor_plan_geometry_source": floor_plan.get("source"),
        "floor_plan_geometry_sha256": floor_plan_geometry_sha256,
        "route_direction_unit": {
            "x": round(direction_unit[0], 9),
            "y": round(direction_unit[1], 9),
        },
        "left_normal_unit": {
            "x": round(left_unit[0], 9),
            "y": round(left_unit[1], 9),
        },
        "obstacle_route_projection_m": round(obstacle_route_projection, 6),
        "obstacle_route_lateral_offset_m": round(
            obstacle_route_lateral_offset,
            6,
        ),
        "longitudinal_buffer_m": (
            _TURTLEBOT3_RECOVERY_REVISION_LONGITUDINAL_BUFFER_M
        ),
        "wide_bbox_clearance_m": wide_bbox_clearance,
        "static_waypoint_clearance_policy_m": (
            _TURTLEBOT3_RECOVERY_REVISION_STATIC_WAYPOINT_CLEARANCE_M
        ),
        "minimum_static_waypoint_clearance_m": (
            round(min(value for value in waypoint_clearances if value is not None), 6)
            if any(value is not None for value in waypoint_clearances)
            else None
        ),
        "computed_detour_distance_m": round(detour_distance, 6),
        "recovery_goal_poses": goal_poses,
        "path_feasibility_claimed": False,
        "claim_boundary": (
            "Rules validate source-bound candidate waypoints against stored simulator "
            "geometry. Nav2 still plans each path; requested-side traversal must be "
            "verified from raw map-frame trajectory after dispatch."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return geometry, []


def _build_return_home_recovery_revision_geometry(
    *,
    proposal: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    map_model = execution.get("turtlebot3_indoor_map_model")
    map_model = map_model if isinstance(map_model, Mapping) else {}
    home_point = next(
        (
            item
            for item in map_model.get("planned_points") or ()
            if isinstance(item, Mapping) and item.get("role") == "home"
        ),
        {},
    )
    home_x = _revision_numeric(home_point.get("x_m"))
    home_y = _revision_numeric(home_point.get("y_m"))
    planned_segments = proposal.get("planned_segments")
    template = next(
        (item for item in planned_segments or () if isinstance(item, Mapping)),
        {},
    )
    if home_x is None or home_y is None or not template:
        return {}, ["operator_recovery_revision_home_pose_source_missing"]
    goal_pose = _revision_goal_payload(
        template=template,
        x_m=home_x,
        y_m=home_y,
        label=str(home_point.get("label") or "simulated_home_origin"),
    )
    return {
        "schema_version": "missionos_turtlebot3_recovery_revision_geometry.v1",
        "geometry_status": "source_bound_candidate",
        "requested_direction": "return_home",
        "home_pose_ref": home_point.get("label") or "map:simulated_home_origin",
        "home_pose_source": "stored_turtlebot3_indoor_map_planned_home_point",
        "recovery_goal_poses": [goal_pose],
        "path_feasibility_claimed": False,
        "claim_boundary": (
            "The exact stored simulator home pose is bound into the checkpoint. "
            "Nav2 completion proves only the bounded return action, not delivery."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }, []


def _build_retry_failed_segment_recovery_revision_geometry(
    *,
    proposal: Mapping[str, Any],
    execution: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Bind one retry to the exact failed route segment and observation."""

    failures = execution.get("route_failure_observation_results")
    failures = failures if isinstance(failures, list) else []
    failed = failures[-1] if failures and isinstance(failures[-1], Mapping) else {}
    next_index = checkpoint.get("next_segment_index")
    planned_segments = proposal.get("planned_segments")
    planned_segments = (
        planned_segments if isinstance(planned_segments, (list, tuple)) else ()
    )
    if (
        not failed
        or failed.get("completion_claimed") is not False
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 1
        or next_index > len(planned_segments)
        or not isinstance(planned_segments[next_index - 1], Mapping)
    ):
        return {}, ["operator_recovery_revision_failed_segment_source_missing"]
    failed_segment_ref = str(failed.get("segment_ref") or "")
    if failed_segment_ref != f"segment_{next_index}":
        return {}, ["operator_recovery_revision_failed_segment_cursor_mismatch"]
    target = planned_segments[next_index - 1]
    target_x = _revision_numeric(target.get("x_m"))
    target_y = _revision_numeric(target.get("y_m"))
    if target_x is None or target_y is None:
        return {}, ["operator_recovery_revision_failed_segment_goal_missing"]
    goal_pose = _revision_goal_payload(
        template=target,
        x_m=target_x,
        y_m=target_y,
        label="operator_revision_retry_failed_segment_once",
    )
    failure_sha256 = hashlib.sha256(
        json.dumps(
            failed,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "missionos_turtlebot3_recovery_revision_geometry.v1",
        "geometry_status": "source_bound_candidate",
        "geometry_strategy": "retry_exact_failed_route_segment_once",
        "requested_direction": "route_retry",
        "failed_segment_ref": failed_segment_ref,
        "failed_segment_observation_sha256": failure_sha256,
        "next_route_goal": {
            "x_m": target_x,
            "y_m": target_y,
            "segment_index": next_index,
            "label": target.get("label"),
            "source": "original_turtlebot3_planned_segment",
        },
        "recovery_goal_poses": [goal_pose],
        "path_feasibility_claimed": False,
        "claim_boundary": (
            "This proposal retries only the exact failed route segment once. "
            "It requires dual-costmap validation, a fresh approval, and a new "
            "runtime observation before the route may continue."
        ),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }, []


def _recovery_revision_blocked_response(
    *,
    status: Literal["unsupported", "blocked"],
    checkpoint: Mapping[str, Any],
    blocking_reasons: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": TURTLEBOT3_RECOVERY_CHECKPOINT_REVISION_SCHEMA,
        "revision_status": status,
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "parent_checkpoint_id": checkpoint.get("checkpoint_id"),
        "parent_checkpoint_hash": checkpoint.get("checkpoint_hash"),
        "superseded_checkpoint": {},
        "turtlebot3_recovery_checkpoint": {},
        "turtlebot3_home_mission_execution": {},
        "summary": {},
        "recovery_proposal": {},
        "recovery_proposal_classification": {},
        "recovery_planner_result": {},
        "operator_approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _validate_operator_revision_recovery_goals(
    *,
    recovery_goal_poses: list[Mapping[str, Any]],
    obstacle_scenario: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Plan-only validate exact operator revision goals on both costmaps."""

    if not _truthy_env(TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV):
        return {}, []
    candidates = [
        {
            "candidate_id": str(
                goal.get("label") or f"operator_revision_waypoint_{index + 1}"
            ),
            "x_m": float(goal["x_m"]),
            "y_m": float(goal["y_m"]),
            "yaw_rad": float(goal.get("yaw_rad") or 0.0),
            "selection_priority": index,
            "geometry_source": "operator_revision_source_bound_geometry",
        }
        for index, goal in enumerate(recovery_goal_poses)
    ]
    candidates = _recovery_sequence_candidates(candidates)
    try:
        evaluation = _evaluate_recovery_candidates_plan_only(
            candidates=candidates,
            obstacle=obstacle_scenario,
            frame_id="map",
        )
    except Ros2Nav2BridgeError as exc:
        return {}, [
            "operator_recovery_revision_dual_costmap_evaluation_failed:"
            f"{type(exc).__name__}"
        ]
    evaluated = [
        dict(item)
        for item in evaluation.get("candidate_evaluations") or []
        if isinstance(item, Mapping)
    ]
    by_id = {str(item.get("candidate_id") or ""): item for item in evaluated}
    dual_validated = (
        bool(candidates)
        and bool(evaluation.get("global_costmap_snapshot_hash"))
        and bool(evaluation.get("local_costmap_snapshot_hash"))
        and all(
            (item := by_id.get(candidate["candidate_id"])) is not None
            and item.get("path_valid") is True
            for candidate in candidates
        )
    )
    if not dual_validated:
        reasons = [
            str(reason)
            for item in evaluated
            for reason in item.get("blocking_reasons") or []
            if str(reason)
        ]
        return {}, list(
            dict.fromkeys(
                reasons
                or ["operator_recovery_revision_not_dual_costmap_validated"]
            )
        )
    sequence = [dict(by_id[candidate["candidate_id"]]) for candidate in candidates]
    return {
        "schema_version": "missionos_nav2_recovery_candidate_resolution.v1",
        "resolution_status": "validated",
        "candidate_generation": "operator_revision_source_bound_geometry.v1",
        "candidates": candidates,
        "candidate_evaluations": evaluated,
        "selected_candidate": dict(sequence[-1]),
        "selected_sequence": sequence,
        "live_costmap_validated": True,
        "dual_costmap_validated": True,
        "bounded_retreat_required": False,
        "costmap_snapshot_hash": evaluation.get("costmap_snapshot_hash"),
        "global_costmap_snapshot_hash": evaluation.get(
            "global_costmap_snapshot_hash"
        ),
        "local_costmap_snapshot_hash": evaluation.get(
            "local_costmap_snapshot_hash"
        ),
        "global_costmap_source": evaluation.get("global_costmap_source"),
        "local_costmap_source": evaluation.get("local_costmap_source"),
        "compute_path_action": evaluation.get("compute_path_action"),
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }, []


def build_turtlebot3_recovery_checkpoint_revision(
    *,
    operator_instruction: str,
    proposal: Mapping[str, Any],
    resume_execution: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a checkpoint-bound natural-language recovery re-proposal.

    This helper never approves or dispatches. Gateway persistence must atomically
    supersede the parent checkpoint and publish the returned child checkpoint.
    """

    checkpoint = _recovery_checkpoint_from_execution(resume_execution)
    execution = _recovery_resume_payload(resume_execution)
    proposal_goals = _planned_segment_goals_from_proposal(proposal)
    proposal_segments_sha256 = _planned_segments_sha256(proposal_goals)
    parent_reasons: list[str] = []
    if checkpoint.get("schema_version") != TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA:
        parent_reasons.append("operator_recovery_revision_parent_checkpoint_invalid")
    if checkpoint.get("checkpoint_status") != "awaiting_operator_approval":
        parent_reasons.append("operator_recovery_revision_parent_checkpoint_not_awaiting")
    checkpoint_hash = str(checkpoint.get("checkpoint_hash") or "")
    if not checkpoint_hash or checkpoint_hash != _recovery_checkpoint_hash(checkpoint):
        parent_reasons.append("operator_recovery_revision_parent_checkpoint_hash_mismatch")
    if str(checkpoint.get("proposal_id") or "") != str(proposal.get("proposal_id") or ""):
        parent_reasons.append("operator_recovery_revision_parent_proposal_mismatch")
    if str(checkpoint.get("planned_segments_sha256") or "") != (
        proposal_segments_sha256
    ):
        parent_reasons.append(
            "operator_recovery_revision_parent_planned_segments_mismatch"
        )
    if str(checkpoint.get("resume_state_hash") or "") != _recovery_resume_state_hash(
        execution
    ):
        parent_reasons.append(
            "operator_recovery_revision_parent_resume_state_hash_mismatch"
        )
    if parent_reasons:
        return _recovery_revision_blocked_response(
            status="blocked",
            checkpoint=checkpoint,
            blocking_reasons=parent_reasons,
        )
    revision_intent, intent_reasons = _turtlebot3_recovery_revision_intent(
        operator_instruction
    )
    if intent_reasons:
        return _recovery_revision_blocked_response(
            status="unsupported",
            checkpoint=checkpoint,
            blocking_reasons=intent_reasons,
        )
    if revision_intent in {"avoid_left_wide", "avoid_right_wide"}:
        geometry, geometry_reasons = _build_directional_recovery_revision_geometry(
            direction="left" if revision_intent == "avoid_left_wide" else "right",
            proposal=proposal,
            execution=execution,
            checkpoint=checkpoint,
        )
        selected_action = "avoid_obstacle"
    elif revision_intent == "retry_failed_segment":
        geometry, geometry_reasons = (
            _build_retry_failed_segment_recovery_revision_geometry(
                proposal=proposal,
                execution=execution,
                checkpoint=checkpoint,
            )
        )
        selected_action = "reroute"
    else:
        geometry, geometry_reasons = _build_return_home_recovery_revision_geometry(
            proposal=proposal,
            execution=execution,
        )
        selected_action = "return_home"
    if geometry_reasons:
        return _recovery_revision_blocked_response(
            status="blocked",
            checkpoint=checkpoint,
            blocking_reasons=geometry_reasons,
        )
    recovery_goal_poses = list(geometry["recovery_goal_poses"])
    revision_candidate_resolution: dict[str, Any] = {}
    if selected_action in {"avoid_obstacle", "reroute"}:
        revision_candidate_resolution, evaluation_reasons = (
            _validate_operator_revision_recovery_goals(
                recovery_goal_poses=recovery_goal_poses,
                obstacle_scenario=(
                    execution.get("runtime_recovery_obstacle_scenario")
                    if isinstance(
                        execution.get("runtime_recovery_obstacle_scenario"),
                        Mapping,
                    )
                    else {}
                ),
            )
        )
        if evaluation_reasons:
            return _recovery_revision_blocked_response(
                status="blocked",
                checkpoint=checkpoint,
                blocking_reasons=evaluation_reasons,
            )
        if revision_candidate_resolution:
            geometry = {
                **geometry,
                "path_feasibility_validated": True,
                "recovery_candidate_resolution": dict(
                    revision_candidate_resolution
                ),
            }

    instruction_hash = hashlib.sha256(
        str(operator_instruction or "").encode("utf-8")
    ).hexdigest()
    revision_seed = json.dumps(
        {
            "parent_checkpoint_hash": checkpoint_hash,
            "operator_instruction_sha256": instruction_hash,
            "revision_intent": revision_intent,
            "geometry": geometry,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    revision_id = (
        "turtlebot3_recovery_revision_"
        + hashlib.sha256(revision_seed.encode("utf-8")).hexdigest()[:12]
    )
    if selected_action == "avoid_obstacle":
        approved_parameters = {
            "recovery_waypoints": [
                {
                    "target_x_m": goal_pose["x_m"],
                    "target_y_m": goal_pose["y_m"],
                }
                for goal_pose in recovery_goal_poses
            ],
            "obstacle_avoidance_required": True,
        }
        reason = (
            "Operator requested a source-bound wide "
            f"{geometry['requested_direction']} avoidance; propose two bounded "
            "Nav2 waypoints and require a fresh approval."
        )
    elif selected_action == "reroute":
        retry_goal = recovery_goal_poses[0]
        approved_parameters = {
            "target_x_m": retry_goal["x_m"],
            "target_y_m": retry_goal["y_m"],
            "retry_failed_segment_required": True,
            "retry_count": 1,
        }
        reason = (
            "Operator acknowledged the source-backed transient stop and requested "
            "one bounded retry of the exact failed route segment; require a fresh "
            "checkpoint-bound approval before dispatch."
        )
    else:
        home_goal = recovery_goal_poses[0]
        approved_parameters = {
            "target_x_m": home_goal["x_m"],
            "target_y_m": home_goal["y_m"],
            "return_home_required": True,
        }
        if revision_intent == "return_home_then_resume":
            approved_parameters["resume_route_after_recovery"] = True
            reason = (
                "Operator explicitly requested a source-bound return to the stored "
                "home pose followed by resumption of the already-approved delivery "
                "route; propose only the bounded return_home action and require a "
                "fresh approval before it is dispatched."
            )
        else:
            reason = (
                "Operator requested a source-bound return to the stored home pose; "
                "propose return_home and require a fresh approval."
            )
    recovery_proposal = build_mission_autonomy_recovery_proposal(
        mission_ref=str(proposal.get("proposal_id") or "turtlebot3_home_mission"),
        proposal_source="operator",
        selected_action=selected_action,
        reason=reason,
        input_observations={
            "revision_id": revision_id,
            "revision_intent": revision_intent,
            "operator_instruction_sha256": instruction_hash,
            "parent_checkpoint_id": checkpoint.get("checkpoint_id"),
            "parent_checkpoint_hash": checkpoint_hash,
            "recovery_revision_geometry": geometry,
        },
    ).model_dump(mode="json")
    classification_seed = (
        f"{checkpoint_hash}\n{recovery_proposal['proposal_id']}\n"
        "requires_human_approval"
    )
    classification = MissionAutonomyProposalClassification(
        classification_id=(
            "mission_autonomy_proposal_classification_"
            + hashlib.sha256(classification_seed.encode("utf-8")).hexdigest()[:12]
        ),
        envelope_ref=str(
            (execution.get("autonomy_envelope") or {}).get("envelope_id")
            if isinstance(execution.get("autonomy_envelope"), Mapping)
            else ""
        )
        or str(checkpoint.get("proposal_id") or ""),
        proposal_ref=str(recovery_proposal["proposal_id"]),
        selected_action=selected_action,
        execution_class="requires_human_approval",
        execution_permitted_by_envelope=False,
        requires_new_human_approval=True,
        blocked_reasons=(
            "operator_requested_recovery_revision_requires_fresh_approval",
        ),
        classification_reason=(
            "The operator constrained a replacement recovery proposal; only a "
            "fresh approval bound to the child checkpoint may dispatch it."
        ),
    ).model_dump(mode="json")
    planner_result = {
        "schema_version": "missionos_turtlebot3_recovery_revision_planner_result.v1",
        "planner_status": "proposed",
        "planner_kind": "operator_constrained_deterministic_geometry",
        "proposal_source": "operator",
        "revision_id": revision_id,
        "revision_intent": revision_intent,
        "proposal": dict(recovery_proposal),
        "guardrail": {
            "guardrail_passed": True,
            "geometry_status": geometry.get("geometry_status"),
            "blocking_reasons": [],
        },
        "llm_invocation_evidence": {},
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    parent_recovery_context = {
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "checkpoint_hash": checkpoint_hash,
        "recovery_proposals": [
            dict(item)
            for item in execution.get("recovery_proposals") or ()
            if isinstance(item, Mapping)
        ],
        "recovery_proposal_classifications": [
            dict(item)
            for item in execution.get("recovery_proposal_classifications") or ()
            if isinstance(item, Mapping)
        ],
        "recovery_planner_result": dict(execution.get("recovery_planner_result") or {})
        if isinstance(execution.get("recovery_planner_result"), Mapping)
        else {},
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    superseded_contexts = [
        dict(item)
        for item in execution.get("superseded_recovery_contexts") or ()
        if isinstance(item, Mapping)
    ]
    updated_execution = {
        **execution,
        "status": "incomplete",
        "recovery_planner_result": planner_result,
        "recovery_planner_status": "proposed",
        "recovery_proposals": [dict(recovery_proposal)],
        "recovery_proposal_classifications": [dict(classification)],
        "recovery_action_suggested": selected_action,
        "runtime_recovery_action_kind": selected_action,
        "recovery_execution_permitted_by_envelope": False,
        "recovery_execution_permitted_by_operator_approval": False,
        "recovery_dispatch_authority_source": None,
        "recovery_dispatch_request_sent": False,
        "recovery_completion_claimed": False,
        "route_resumed_after_recovery": False,
        "route_completed_after_recovery": False,
        "superseded_recovery_contexts": [
            *superseded_contexts,
            parent_recovery_context,
        ],
    }
    if revision_candidate_resolution:
        updated_execution["recovery_candidate_resolution"] = dict(
            revision_candidate_resolution
        )
    resume_state_hash = _recovery_resume_state_hash(updated_execution)
    new_checkpoint: dict[str, Any] = {
        "schema_version": TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA,
        "checkpoint_status": "awaiting_operator_approval",
        "proposal_id": str(checkpoint.get("proposal_id") or ""),
        "robot_profile": str(checkpoint.get("robot_profile") or ""),
        "execution_target": str(checkpoint.get("execution_target") or ""),
        "recovery_proposal_id": str(recovery_proposal["proposal_id"]),
        "recovery_classification_id": str(classification["classification_id"]),
        "selected_action": selected_action,
        "approved_parameters": approved_parameters,
        "recovery_goal_poses": recovery_goal_poses,
        "completed_segment_count": checkpoint.get("completed_segment_count"),
        "next_segment_index": checkpoint.get("next_segment_index"),
        "remaining_segment_count": checkpoint.get("remaining_segment_count"),
        "planned_segments_sha256": proposal_segments_sha256,
        "resume_state_hash": resume_state_hash,
        "parent_checkpoint_id": checkpoint.get("checkpoint_id"),
        "parent_checkpoint_hash": checkpoint_hash,
        "parent_recovery_context": parent_recovery_context,
        "revision_id": revision_id,
        "revision_intent": revision_intent,
        "operator_instruction_sha256": instruction_hash,
        "recovery_revision_geometry": geometry,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    if revision_candidate_resolution:
        sequence = revision_candidate_resolution["selected_sequence"]
        new_checkpoint["recovery_candidate_binding"] = {
            "candidate_id": sequence[-1]["candidate_id"],
            "candidate_ids": [item["candidate_id"] for item in sequence],
            "path_sha256": sequence[-1].get("path_sha256"),
            "path_sha256_sequence": [
                item.get("path_sha256") for item in sequence
            ],
            "costmap_snapshot_hash": revision_candidate_resolution.get(
                "costmap_snapshot_hash"
            ),
            "global_costmap_snapshot_hash": revision_candidate_resolution.get(
                "global_costmap_snapshot_hash"
            ),
            "local_costmap_snapshot_hash": revision_candidate_resolution.get(
                "local_costmap_snapshot_hash"
            ),
            "live_costmap_validated": True,
            "dual_costmap_validated": True,
            "bounded_retreat_required": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    new_checkpoint["recovery_contract_bundle"] = (
        build_turtlebot3_recovery_contract_bundle(new_checkpoint)
    )
    new_checkpoint_hash = _recovery_checkpoint_hash(new_checkpoint)
    new_checkpoint["checkpoint_hash"] = new_checkpoint_hash
    new_checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{new_checkpoint_hash[:12]}"
    )
    revised_at = (now or datetime.now(timezone.utc)).isoformat()
    superseded_checkpoint = {
        **checkpoint,
        "checkpoint_status": "superseded",
        "superseded_at": revised_at,
        "superseded_by_checkpoint_id": new_checkpoint["checkpoint_id"],
        "superseded_by_checkpoint_hash": new_checkpoint["checkpoint_hash"],
        "superseded_by_revision_id": revision_id,
        "superseded_by_revision_ref": revision_id,
    }
    updated_execution["turtlebot3_recovery_checkpoint"] = dict(new_checkpoint)
    updated_execution["recovery_checkpoint_revision"] = {
        "revision_id": revision_id,
        "parent_checkpoint_id": checkpoint.get("checkpoint_id"),
        "child_checkpoint_id": new_checkpoint["checkpoint_id"],
        "revision_intent": revision_intent,
        "operator_approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    summary = {
        "status": "incomplete",
        "recovery_planner_result": planner_result,
        "recovery_planner_status": "proposed",
        "recovery_proposals": [dict(recovery_proposal)],
        "recovery_proposal_classifications": [dict(classification)],
        "recovery_action_suggested": selected_action,
        "runtime_recovery_action_kind": selected_action,
        "turtlebot3_recovery_checkpoint": dict(new_checkpoint),
        "recovery_checkpoint_revision": dict(
            updated_execution["recovery_checkpoint_revision"]
        ),
        "recovery_execution_permitted_by_envelope": False,
        "recovery_execution_permitted_by_operator_approval": False,
        "recovery_dispatch_authority_source": None,
        "recovery_dispatch_request_sent": False,
        "recovery_completion_claimed": False,
        "route_resumed_after_recovery": False,
        "route_completed_after_recovery": False,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    if revision_candidate_resolution:
        summary["recovery_candidate_resolution"] = dict(
            revision_candidate_resolution
        )
    return {
        "schema_version": TURTLEBOT3_RECOVERY_CHECKPOINT_REVISION_SCHEMA,
        "revision_status": "proposed",
        "revision_id": revision_id,
        "blocking_reasons": [],
        "parent_checkpoint_id": checkpoint.get("checkpoint_id"),
        "parent_checkpoint_hash": checkpoint_hash,
        "parent_recovery_context": parent_recovery_context,
        "superseded_checkpoint": superseded_checkpoint,
        "turtlebot3_recovery_checkpoint": new_checkpoint,
        "turtlebot3_home_mission_execution": updated_execution,
        "summary": summary,
        "recovery_proposal": recovery_proposal,
        "recovery_proposal_classification": classification,
        "recovery_planner_result": planner_result,
        "operator_approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _validate_turtlebot3_recovery_resume(
    *,
    checkpoint: Mapping[str, Any],
    resume_state: Mapping[str, Any],
    proposal: Mapping[str, Any],
    goals: tuple[Nav2GoalPose, ...],
    recovery_operator_approval: Mapping[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    if checkpoint.get("schema_version") != TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA:
        reasons.append("turtlebot3_recovery_checkpoint_schema_invalid")
    if checkpoint.get("checkpoint_status") != "awaiting_operator_approval":
        reasons.append("turtlebot3_recovery_checkpoint_not_awaiting_approval")
    checkpoint_hash = str(checkpoint.get("checkpoint_hash") or "")
    if not checkpoint_hash or checkpoint_hash != _recovery_checkpoint_hash(checkpoint):
        reasons.append("turtlebot3_recovery_checkpoint_hash_mismatch")
    reasons.extend(validate_turtlebot3_recovery_contract_bundle(checkpoint))
    expected_checkpoint_id = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
        if checkpoint_hash
        else ""
    )
    if str(checkpoint.get("checkpoint_id") or "") != expected_checkpoint_id:
        reasons.append("turtlebot3_recovery_checkpoint_id_mismatch")
    if str(checkpoint.get("proposal_id") or "") != str(
        proposal.get("proposal_id") or ""
    ):
        reasons.append("turtlebot3_recovery_checkpoint_proposal_mismatch")
    if str(checkpoint.get("robot_profile") or "") != _robot_profile_from_proposal(
        proposal
    ):
        reasons.append("turtlebot3_recovery_checkpoint_robot_profile_mismatch")
    if str(checkpoint.get("execution_target") or "") != str(
        proposal.get("execution_target") or ""
    ):
        reasons.append("turtlebot3_recovery_checkpoint_execution_target_mismatch")
    if str(checkpoint.get("planned_segments_sha256") or "") != (
        _planned_segments_sha256(goals)
    ):
        reasons.append("turtlebot3_recovery_checkpoint_planned_segments_mismatch")
    completed_count = checkpoint.get("completed_segment_count")
    next_index = checkpoint.get("next_segment_index")
    remaining_count = checkpoint.get("remaining_segment_count")
    if (
        not isinstance(completed_count, int)
        or isinstance(completed_count, bool)
        or completed_count < 0
        or completed_count > len(goals)
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index != completed_count + 1
        or not isinstance(remaining_count, int)
        or isinstance(remaining_count, bool)
        or remaining_count != len(goals) - completed_count
    ):
        reasons.append("turtlebot3_recovery_checkpoint_segment_cursor_invalid")
    if str(checkpoint.get("resume_state_hash") or "") != _recovery_resume_state_hash(
        resume_state
    ):
        reasons.append("turtlebot3_recovery_checkpoint_resume_state_hash_mismatch")
    stored_results = resume_state.get("segment_results")
    if not isinstance(stored_results, list) or len(stored_results) != completed_count:
        reasons.append("turtlebot3_recovery_checkpoint_segment_results_invalid")

    approval_payload = (
        recovery_operator_approval
        if isinstance(recovery_operator_approval, Mapping)
        else {}
    )
    if (
        approval_payload.get("schema_version")
        != TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_SCHEMA
    ):
        reasons.append("turtlebot3_recovery_operator_approval_schema_invalid")
    if (
        approval_payload.get("operator_approved") is not True
        or approval_payload.get("explicit_recovery_dispatch_approval") is not True
        or not str(approval_payload.get("operator_approval_ref") or "").strip()
    ):
        reasons.append("turtlebot3_recovery_operator_approval_missing")
    bindings = (
        ("checkpoint_id", "checkpoint_id"),
        ("checkpoint_hash", "checkpoint_hash"),
        ("recovery_proposal_id", "recovery_proposal_id"),
        ("recovery_classification_id", "recovery_classification_id"),
        ("approved_action", "selected_action"),
    )
    for approval_key, checkpoint_key in bindings:
        if str(approval_payload.get(approval_key) or "") != str(
            checkpoint.get(checkpoint_key) or ""
        ):
            reasons.append(
                f"turtlebot3_recovery_operator_approval_{approval_key}_mismatch"
            )
    approved_parameters = approval_payload.get("approved_parameters")
    if not isinstance(approved_parameters, Mapping) or dict(approved_parameters) != dict(
        checkpoint.get("approved_parameters") or {}
    ):
        reasons.append("turtlebot3_recovery_operator_approval_parameters_mismatch")
    selected_action = str(checkpoint.get("selected_action") or "")
    approved_parameters = checkpoint.get("approved_parameters")
    approved_parameters = (
        approved_parameters if isinstance(approved_parameters, Mapping) else {}
    )
    recovery_goal_payloads = checkpoint.get("recovery_goal_poses")
    recovery_goal_payloads = (
        recovery_goal_payloads if isinstance(recovery_goal_payloads, list) else []
    )
    if selected_action == "avoid_obstacle":
        if recovery_goal_payloads:
            waypoints = approved_parameters.get("recovery_waypoints")
            if (
                len(recovery_goal_payloads) not in {1, 2}
                or not isinstance(waypoints, list)
                or len(waypoints) != len(recovery_goal_payloads)
            ):
                reasons.append(
                    "turtlebot3_recovery_checkpoint_waypoint_parameters_invalid"
                )
            else:
                for waypoint, goal_payload in zip(
                    waypoints,
                    recovery_goal_payloads,
                    strict=True,
                ):
                    if not isinstance(waypoint, Mapping) or not isinstance(
                        goal_payload, Mapping
                    ):
                        reasons.append(
                            "turtlebot3_recovery_checkpoint_waypoint_parameters_invalid"
                        )
                        break
                    if (
                        _revision_numeric(waypoint.get("target_x_m"))
                        != _revision_numeric(goal_payload.get("x_m"))
                        or _revision_numeric(waypoint.get("target_y_m"))
                        != _revision_numeric(goal_payload.get("y_m"))
                    ):
                        reasons.append(
                            "turtlebot3_recovery_checkpoint_waypoint_parameters_mismatch"
                        )
                        break
        elif not approved_parameters:
            reasons.append("turtlebot3_recovery_checkpoint_action_not_supported")
    elif selected_action == "reroute":
        if (
            set(approved_parameters)
            != {
                "target_x_m",
                "target_y_m",
                "retry_failed_segment_required",
                "retry_count",
            }
            or approved_parameters.get("retry_failed_segment_required") is not True
            or approved_parameters.get("retry_count") != 1
            or len(recovery_goal_payloads) != 1
            or not isinstance(recovery_goal_payloads[0], Mapping)
            or _revision_numeric(approved_parameters.get("target_x_m"))
            != _revision_numeric(recovery_goal_payloads[0].get("x_m"))
            or _revision_numeric(approved_parameters.get("target_y_m"))
            != _revision_numeric(recovery_goal_payloads[0].get("y_m"))
        ):
            reasons.append("turtlebot3_recovery_checkpoint_reroute_parameters_invalid")
    elif selected_action == "return_home":
        resume_route_after_recovery = approved_parameters.get(
            "resume_route_after_recovery"
        )
        if (
            len(recovery_goal_payloads) != 1
            or approved_parameters.get("return_home_required") is not True
            or resume_route_after_recovery not in {None, True}
        ):
            reasons.append("turtlebot3_recovery_checkpoint_return_home_parameters_invalid")
        elif isinstance(recovery_goal_payloads[0], Mapping):
            home_goal = recovery_goal_payloads[0]
            if (
                _revision_numeric(approved_parameters.get("target_x_m"))
                != _revision_numeric(home_goal.get("x_m"))
                or _revision_numeric(approved_parameters.get("target_y_m"))
                != _revision_numeric(home_goal.get("y_m"))
            ):
                reasons.append(
                    "turtlebot3_recovery_checkpoint_return_home_parameters_mismatch"
                )
            map_model = resume_state.get("turtlebot3_indoor_map_model")
            map_model = map_model if isinstance(map_model, Mapping) else {}
            stored_home = next(
                (
                    item
                    for item in map_model.get("planned_points") or ()
                    if isinstance(item, Mapping) and item.get("role") == "home"
                ),
                {},
            )
            if (
                _revision_numeric(stored_home.get("x_m"))
                != _revision_numeric(home_goal.get("x_m"))
                or _revision_numeric(stored_home.get("y_m"))
                != _revision_numeric(home_goal.get("y_m"))
            ):
                reasons.append(
                    "turtlebot3_recovery_checkpoint_return_home_pose_mismatch"
                )
    else:
        reasons.append("turtlebot3_recovery_checkpoint_action_not_supported")
    reasons.extend(
        _turtlebot3_recovery_revision_source_geometry_reasons(
            checkpoint=checkpoint,
            execution=resume_state,
        )
    )
    for goal_payload in recovery_goal_payloads:
        if not isinstance(goal_payload, Mapping):
            reasons.append("turtlebot3_recovery_checkpoint_goal_pose_invalid")
            continue
        try:
            validated_goal = Nav2GoalPose.model_validate(dict(goal_payload))
        except ValueError:
            reasons.append("turtlebot3_recovery_checkpoint_goal_pose_invalid")
            continue
        if validated_goal.frame_id != "map":
            reasons.append("turtlebot3_recovery_checkpoint_goal_frame_invalid")
    return list(dict.fromkeys(reasons))


def _recovery_goals_from_checkpoint(
    checkpoint: Mapping[str, Any],
) -> tuple[Nav2GoalPose, ...]:
    payloads = checkpoint.get("recovery_goal_poses")
    if isinstance(payloads, list) and payloads:
        return tuple(
            Nav2GoalPose.model_validate(dict(payload))
            for payload in payloads
            if isinstance(payload, Mapping)
        )
    parameters = checkpoint.get("approved_parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    if not {"target_x_m", "target_y_m"}.issubset(parameters):
        return ()
    template = _profile_dynamic_obstacle_avoidance_goal()
    return (
        template.model_copy(
            update={
                "x_m": float(parameters["target_x_m"]),
                "y_m": float(parameters["target_y_m"]),
                "yaw_rad": float(parameters.get("target_yaw_rad") or 0.0),
            }
        ),
    )


def _recovery_goal_from_checkpoint(checkpoint: Mapping[str, Any]) -> Nav2GoalPose:
    """Compatibility accessor for callers that still expect one recovery goal."""

    return _recovery_goals_from_checkpoint(checkpoint)[0]


def run_turtlebot3_home_mission_dispatch(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    now: datetime | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    resume_execution: Mapping[str, Any] | None = None,
    recovery_operator_approval: Mapping[str, Any] | None = None,
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
    route_authority = approval.get("route_authority")
    route_authority = (
        dict(route_authority) if isinstance(route_authority, Mapping) else None
    )
    blocking_reasons.extend(
        validate_turtlebot3_route_authority_binding(
            binding=route_authority,
            proposal_id=str(
                proposal.get("proposal_id") or "turtlebot3_home_mission"
            ),
            operator_approval_ref=approval_ref,
            approved_scope=str(approval.get("approved_scope") or ""),
            planned_segments=goals,
            autonomy_envelope=autonomy_envelope,
        )
    )

    segment_results: list[dict[str, Any]] = []
    segment_transition_authority_records: list[dict[str, Any]] = []
    route_failure_observation_results: list[dict[str, Any]] = []
    recovery_segment_result: dict[str, Any] = {}
    prior_recovery_segment_results: list[dict[str, Any]] = []
    approved_recovery_segment_results: list[dict[str, Any]] = []
    subsequent_recovery_segment_results: list[dict[str, Any]] = []
    recovery_closed_loop_cycles: list[dict[str, Any]] = []
    recovery_requested_side_observation: dict[str, Any] = {}
    recovery_goal_sequence_completed = False
    recovery_checkpoint = _recovery_checkpoint_from_execution(resume_execution)
    recovery_repair_parent_checkpoint: dict[str, Any] = {}
    recovery_followup_parent_checkpoint: dict[str, Any] = {}
    recovery_resume_state = _recovery_resume_payload(resume_execution)
    resume_requested = resume_execution is not None
    start_segment_index = 1
    pre_recovery_segment_result_count = 0

    def _recovery_runtime_status_projection() -> dict[str, Any]:
        latest_recovery_result = (
            approved_recovery_segment_results[-1]
            if approved_recovery_segment_results
            else recovery_segment_result
        )
        latest_recovery_result = (
            latest_recovery_result
            if isinstance(latest_recovery_result, Mapping)
            else {}
        )
        latest_recovery_responses = latest_recovery_result.get("bridge_responses")
        latest_recovery_responses = (
            latest_recovery_responses
            if isinstance(latest_recovery_responses, list)
            else []
        )
        latest_recovery_response = (
            latest_recovery_responses[-1]
            if latest_recovery_responses
            and isinstance(latest_recovery_responses[-1], Mapping)
            else {}
        )
        goal_status = str(
            latest_recovery_response.get("nav2_status")
            or ("not_dispatched" if not latest_recovery_result else "unknown")
        )
        expected_recovery_goal_count = (
            max(len(_recovery_goals_from_checkpoint(recovery_checkpoint)), 1)
            if recovery_checkpoint
            else 1
        )
        recovery_sequence_in_progress = (
            bool(approved_recovery_segment_results)
            and len(approved_recovery_segment_results)
            < expected_recovery_goal_count
            and all(
                item.get("completion_claimed") is True
                for item in approved_recovery_segment_results
            )
        )
        if recovery_sequence_in_progress:
            goal_status = "sequence_in_progress"
        goal_succeeded = latest_recovery_result.get("completion_claimed") is True
        verification_status = (
            "verified"
            if route_resumed_after_recovery
            else "sequence_in_progress"
            if recovery_sequence_in_progress
            else "goal_succeeded_pending_route_resume"
            if goal_succeeded
            else "failed"
            if latest_recovery_result
            and latest_recovery_result.get("completion_claimed") is False
            else "pending"
        )
        return {
            "recovery_goal_status": goal_status,
            "recovery_goal_succeeded_observed": goal_succeeded,
            "recovery_verification_status": verification_status,
            "route_resume_status": (
                "resumed" if route_resumed_after_recovery else "not_resumed"
            ),
        }

    def _emit_progress(
        *,
        runtime_recovery_triggered: bool = False,
        recovery_action_suggested: str | None = None,
    ) -> None:
        if progress_callback is None:
            return
        try:
            recovery_status = _recovery_runtime_status_projection()
            partial_action_results = [
                *segment_results,
                *prior_recovery_segment_results,
                *approved_recovery_segment_results,
                *subsequent_recovery_segment_results,
            ]
            partial_recovery_dispatch_sent = any(
                item.get("dispatch_request_sent") is True
                for item in approved_recovery_segment_results
            )
            partial_recovery_verified = (
                bool(approved_recovery_segment_results)
                and all(
                    item.get("completion_claimed") is True
                    for item in approved_recovery_segment_results
                )
                and route_resumed_after_recovery
            )
            partial_map = _build_turtlebot3_indoor_map_model(
                proposal=proposal,
                goals=goals,
                segment_results=segment_results,
                recovery_segment_result=recovery_segment_result,
                approved_recovery_segment_results=(
                    [
                        *prior_recovery_segment_results,
                        *approved_recovery_segment_results,
                    ]
                ),
                subsequent_recovery_segment_results=(
                    subsequent_recovery_segment_results
                ),
                status="running",
                obstacle_required=_obstacle_challenge_required(proposal),
                obstacle={},
                motion={},
                runtime_recovery_triggered=runtime_recovery_triggered,
                recovery_action_suggested=recovery_action_suggested,
                route_resumed_after_recovery=route_resumed_after_recovery,
            )
            partial_map["recovery"]["completion_claimed"] = False
            partial_map["recovery"].update(
                {
                    "goal_status": recovery_status["recovery_goal_status"],
                    "goal_succeeded_observed": recovery_status[
                        "recovery_goal_succeeded_observed"
                    ],
                    "verification_status": recovery_status[
                        "recovery_verification_status"
                    ],
                    "route_resume_status": recovery_status["route_resume_status"],
                }
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
                        **recovery_status,
                        "recovery_dispatch_request_sent": (
                            partial_recovery_dispatch_sent
                        ),
                        "recovery_completion_claimed": partial_recovery_verified,
                        "route_resumed_after_recovery": (
                            route_resumed_after_recovery
                        ),
                        "robot_motion_observed": any(
                            item.get("robot_motion_observed") is True
                            for item in partial_action_results
                        ),
                        "odom_delta_m": _sum_numeric(
                            [
                                item.get("odom_delta_m")
                                for item in partial_action_results
                            ]
                        ),
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
    recovery_candidate_resolution: dict[str, Any] = {}
    recovery_candidate_revalidation: dict[str, Any] = {}
    runtime_recovery_action_kind: str | None = None
    runtime_failure_context: dict[str, Any] = {}
    route_resumed_after_recovery = False
    if resume_requested:
        resume_blocking_reasons = _validate_turtlebot3_recovery_resume(
            checkpoint=recovery_checkpoint,
            resume_state=recovery_resume_state,
            proposal=proposal,
            goals=goals,
            recovery_operator_approval=recovery_operator_approval,
        )
        stored_segment_results = recovery_resume_state.get("segment_results")
        if isinstance(stored_segment_results, list):
            segment_results = [
                dict(item) for item in stored_segment_results if isinstance(item, Mapping)
            ]
            pre_recovery_segment_result_count = len(segment_results)
        stored_failure_observations = recovery_resume_state.get(
            "route_failure_observation_results"
        )
        if isinstance(stored_failure_observations, list):
            route_failure_observation_results = [
                dict(item)
                for item in stored_failure_observations
                if isinstance(item, Mapping)
            ]
        stored_closed_loop_cycles = recovery_resume_state.get(
            "recovery_closed_loop_cycles"
        )
        if isinstance(stored_closed_loop_cycles, list):
            recovery_closed_loop_cycles = [
                dict(item)
                for item in stored_closed_loop_cycles
                if isinstance(item, Mapping)
            ]
        stored_recovery_history = recovery_resume_state.get(
            "recovery_attempt_history"
        )
        if not isinstance(stored_recovery_history, list):
            stored_recovery_history = recovery_resume_state.get(
                "approved_recovery_segment_results"
            )
        if isinstance(stored_recovery_history, list):
            prior_recovery_segment_results = [
                dict(item)
                for item in stored_recovery_history
                if isinstance(item, Mapping)
            ]
        stored_proposals = recovery_resume_state.get("recovery_proposals")
        recovery_proposals = (
            tuple(
                dict(item) for item in stored_proposals if isinstance(item, Mapping)
            )
            if isinstance(stored_proposals, list)
            else ()
        )
        stored_classifications = recovery_resume_state.get(
            "recovery_proposal_classifications"
        )
        recovery_proposal_classifications = (
            tuple(
                dict(item)
                for item in stored_classifications
                if isinstance(item, Mapping)
            )
            if isinstance(stored_classifications, list)
            else ()
        )
        stored_planner_result = recovery_resume_state.get("recovery_planner_result")
        recovery_planner_result = (
            dict(stored_planner_result)
            if isinstance(stored_planner_result, Mapping)
            else {}
        )
        stored_obstacle = recovery_resume_state.get(
            "runtime_recovery_obstacle_scenario"
        )
        runtime_recovery_obstacle_scenario = (
            dict(stored_obstacle) if isinstance(stored_obstacle, Mapping) else {}
        )
        stored_candidate_resolution = runtime_recovery_obstacle_scenario.get(
            "recovery_candidate_resolution"
        )
        recovery_candidate_resolution = (
            dict(stored_candidate_resolution)
            if isinstance(stored_candidate_resolution, Mapping)
            else {}
        )
        stored_motion = recovery_resume_state.get("runtime_recovery_motion_context")
        runtime_recovery_motion_context = (
            dict(stored_motion) if isinstance(stored_motion, Mapping) else {}
        )
        recovery_action_suggested = str(
            recovery_checkpoint.get("selected_action") or ""
        ) or None
        runtime_recovery_action_kind = recovery_action_suggested
        runtime_recovery_triggered = True
        recovery_execution_permitted_by_envelope = False
        recovery_candidate_revalidation = (
            _revalidate_approved_recovery_candidate(
                checkpoint=recovery_checkpoint,
                obstacle_scenario=runtime_recovery_obstacle_scenario,
            )
        )
        if recovery_candidate_revalidation.get("revalidation_status") == "blocked":
            resume_blocking_reasons.extend(
                list(
                    recovery_candidate_revalidation.get("blocking_reasons")
                    or ["approved_recovery_candidate_revalidation_blocked"]
                )
            )
        blocking_reasons.extend(resume_blocking_reasons)
        if resume_blocking_reasons:
            recovery_checkpoint = {
                **recovery_checkpoint,
                "checkpoint_status": "failed",
                "failed_at": dispatched_at.isoformat(),
                "failure_reasons": list(resume_blocking_reasons),
            }
            if recovery_candidate_revalidation.get("revalidation_status") == "blocked":
                recovery_repair_parent_checkpoint = dict(recovery_checkpoint)
                predispatch_observation = {
                    "segment_ref": "recovery_predispatch_revalidation",
                    "completion_claimed": False,
                    "dispatch_request_sent": False,
                    "command_ack_observed": False,
                    "robot_motion_observed": False,
                    "odom_delta_m": 0.0,
                    "costmap_obstacle_observed": True,
                    "obstacle_detected": True,
                    "blocking_reasons": list(resume_blocking_reasons),
                    "bridge_responses": [],
                }
                (
                    recovery_checkpoint,
                    runtime_recovery_obstacle_scenario,
                    recovery_planner_result,
                    recovery_proposals,
                    recovery_proposal_classifications,
                    runtime_recovery_motion_context,
                ) = _build_recovery_failure_followup_checkpoint(
                    parent_checkpoint=recovery_repair_parent_checkpoint,
                    proposal=proposal,
                    goals=goals,
                    segment_results=segment_results,
                    approved_recovery_results=[predispatch_observation],
                    route_failure_observation_results=(
                        route_failure_observation_results
                    ),
                    recovery_closed_loop_cycles=recovery_closed_loop_cycles,
                    autonomy_envelope=autonomy_envelope,
                    battery_envelope=battery_envelope,
                    failure_source=(
                        "approved_recovery_predispatch_revalidation"
                    ),
                )
                recovery_action_suggested = str(
                    recovery_checkpoint.get("selected_action") or ""
                )
                runtime_recovery_action_kind = recovery_action_suggested
                recovery_candidate_resolution = dict(
                    runtime_recovery_obstacle_scenario.get(
                        "recovery_candidate_resolution"
                    )
                    or {}
                )
        else:
            start_segment_index = int(recovery_checkpoint["next_segment_index"])
            approval_payload = dict(recovery_operator_approval or {})
            approval_payload.setdefault("approval_status", "approved")
            approval_payload.setdefault(
                "approval_actor", "missionos_chat_operator"
            )
            approval_payload.setdefault("approved_at", dispatched_at.isoformat())
            approval_payload.setdefault(
                "proposal_ref", recovery_checkpoint.get("recovery_proposal_id")
            )
            approval_payload.setdefault(
                "classification_ref",
                recovery_checkpoint.get("recovery_classification_id"),
            )
            approval_payload["requires_new_human_approval_satisfied"] = True
            approval_payload["dispatch_authority_created_by_operator_approval"] = True
            approval_payload["proposal_dispatch_authority_created"] = False
            approval_payload["physical_execution_invoked"] = False
            approval_payload["mission_delivery_completion_claimed"] = False
            approval_payload["progress_counted"] = False
            fresh_recovery_operator_approvals.append(approval_payload)
            recovery_execution_permitted_by_operator_approval = True
            recovery_dispatch_authority_source = "fresh_operator_approval"
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
        if resume_requested:
            recovery_approval = fresh_recovery_operator_approvals[-1]
            recovery_goals = _recovery_goals_from_checkpoint(recovery_checkpoint)
            selected_recovery_action = str(
                recovery_checkpoint.get("selected_action") or ""
            )
            for recovery_goal_index, recovery_goal in enumerate(
                recovery_goals,
                start=1,
            ):
                recovery_result = _dispatch_nav2_goal(
                    proposal=proposal,
                    approval=recovery_approval,
                    goal=recovery_goal,
                    approval_ref=str(recovery_approval["operator_approval_ref"]),
                    dispatched_at=dispatched_at,
                    action_ref_suffix=(
                        "recovery_return_home"
                        if selected_recovery_action == "return_home"
                        else "recovery_reroute_failed_segment"
                        if selected_recovery_action == "reroute"
                        else f"recovery_avoid_obstacle_waypoint_{recovery_goal_index}"
                    ),
                    publish_initialpose=False,
                )
                approved_recovery_segment_results.append(recovery_result)
                recovery_segment_result = recovery_result
                evidence = recovery_result["adapter_evidence"]
                _emit_progress(
                    runtime_recovery_triggered=True,
                    recovery_action_suggested=selected_recovery_action,
                )
                if recovery_result.get("completion_claimed") is not True:
                    break
            recovery_goal_sequence_completed = bool(
                approved_recovery_segment_results
            ) and len(approved_recovery_segment_results) == len(recovery_goals) and all(
                result.get("completion_claimed") is True
                for result in approved_recovery_segment_results
            )
            recovery_requested_side_observation = (
                _recovery_requested_side_observation(
                    checkpoint=recovery_checkpoint,
                    approved_recovery_results=approved_recovery_segment_results,
                )
            )
            immediate_side_verification_required = (
                recovery_requested_side_observation.get("observation_status")
                != "not_required"
            )
            immediate_side_verification_satisfied = (
                not immediate_side_verification_required
                or recovery_requested_side_observation.get("requested_side_observed")
                is True
            )
            immediate_clearance_verification_required = (
                selected_recovery_action == "avoid_obstacle"
                and _obstacle_challenge_required(proposal)
            )
            immediate_clearance_observation = (
                _obstacle_trajectory_geometry(
                    obstacle_required=True,
                    obstacle={
                        "costmap_obstacle_observed": any(
                            item.get("costmap_obstacle_observed") is True
                            for item in approved_recovery_segment_results
                        ),
                        "obstacle_avoidance_observed": any(
                            item.get("obstacle_avoidance_observed") is True
                            for item in approved_recovery_segment_results
                        ),
                    },
                    observed_points=[],
                    recovery_points=[
                        point
                        for item in approved_recovery_segment_results
                        for point in _observed_points_from_action_result(item)
                    ],
                    robot_profile=_robot_profile_from_proposal(proposal),
                )
                if immediate_clearance_verification_required
                else {
                    "obstacle_trajectory_clearance_observed": True,
                    "obstacle_trajectory_intersects_obstacle": False,
                    "obstacle_trajectory_geometry_status": "not_required",
                }
            )
            immediate_clearance_verification_satisfied = (
                not immediate_clearance_verification_required
                or immediate_clearance_observation.get(
                    "obstacle_trajectory_clearance_observed"
                )
                is True
            )
            recovery_action_completion_verified = (
                recovery_goal_sequence_completed
                and immediate_side_verification_satisfied
                and immediate_clearance_verification_satisfied
            )
            approved_recovery_parameters = recovery_checkpoint.get(
                "approved_parameters"
            )
            approved_recovery_parameters = (
                approved_recovery_parameters
                if isinstance(approved_recovery_parameters, Mapping)
                else {}
            )
            route_resume_explicitly_approved = (
                selected_recovery_action in {"avoid_obstacle", "reroute"}
                or (
                    selected_recovery_action == "return_home"
                    and approved_recovery_parameters.get(
                        "resume_route_after_recovery"
                    )
                    is True
                )
            )
            recovery_outcome_verification = verify_turtlebot3_recovery_outcome(
                checkpoint=recovery_checkpoint,
                operator_approval=recovery_approval,
                action_results=approved_recovery_segment_results,
                goal_sequence_completed=recovery_goal_sequence_completed,
                requested_side_required=immediate_side_verification_required,
                requested_side_observed=(
                    recovery_requested_side_observation.get(
                        "requested_side_observed"
                    )
                    is True
                ),
                obstacle_clearance_required=(
                    immediate_clearance_verification_required
                ),
                obstacle_clearance_observed=(
                    immediate_clearance_observation.get(
                        "obstacle_trajectory_clearance_observed"
                    )
                    is True
                ),
                route_resume_explicitly_approved=(
                    route_resume_explicitly_approved
                ),
            )
            route_resumed_after_recovery = (
                recovery_outcome_verification.get("route_resume_authorized")
                is True
            )
            recovery_goal_observed_at = datetime.now(timezone.utc).isoformat()
            recovery_observation_payload = {
                "checkpoint_id": recovery_checkpoint.get("checkpoint_id"),
                "selected_action": selected_recovery_action,
                "results": [dict(item) for item in approved_recovery_segment_results],
            }
            recovery_observation_sha256 = hashlib.sha256(
                json.dumps(
                    recovery_observation_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            recovery_closed_loop_cycles.append(
                {
                    "schema_version": (
                        "missionos_turtlebot3_recovery_closed_loop_cycle.v1"
                    ),
                    "cycle_index": len(recovery_closed_loop_cycles) + 1,
                    "checkpoint_id": recovery_checkpoint.get("checkpoint_id"),
                    "checkpoint_hash": recovery_checkpoint.get("checkpoint_hash"),
                    "proposal_id": recovery_checkpoint.get("recovery_proposal_id"),
                    "classification_id": recovery_checkpoint.get(
                        "recovery_classification_id"
                    ),
                    "selected_action": selected_recovery_action,
                    "operator_approval_ref": recovery_approval.get(
                        "operator_approval_ref"
                    ),
                    "bounded_action_result_count": len(
                        approved_recovery_segment_results
                    ),
                    "dispatch_request_sent": any(
                        item.get("dispatch_request_sent") is True
                        for item in approved_recovery_segment_results
                    ),
                    "action_materialized": any(
                        (
                            item.get("adapter_evidence")
                            if isinstance(item.get("adapter_evidence"), Mapping)
                            else {}
                        ).get("command_ack_observed")
                        is True
                        for item in approved_recovery_segment_results
                    ),
                    "reobservation_status": (
                        "verified"
                        if recovery_action_completion_verified
                        else "failed"
                    ),
                    "reobservation_sha256": recovery_observation_sha256,
                    "outcome_verification": recovery_outcome_verification,
                    "outcome_verification_id": (
                        recovery_outcome_verification.get(
                            "recovery_outcome_verification_id"
                        )
                    ),
                    "outcome_verification_sha256": (
                        recovery_outcome_verification.get(
                            "recovery_outcome_verification_sha256"
                        )
                    ),
                    "behavior_delta": {
                        "robot_motion_observed": any(
                            item.get("robot_motion_observed") is True
                            for item in approved_recovery_segment_results
                        ),
                        "odom_delta_m": _sum_numeric(
                            [
                                item.get("odom_delta_m")
                                for item in approved_recovery_segment_results
                            ]
                        ),
                        "route_resume_status": (
                            "resumed"
                            if route_resumed_after_recovery
                            else "not_resumed"
                        ),
                        "obstacle_trajectory_clearance_observed": (
                            immediate_clearance_observation.get(
                                "obstacle_trajectory_clearance_observed"
                            )
                            is True
                        ),
                        "obstacle_trajectory_intersects_obstacle": (
                            immediate_clearance_observation.get(
                                "obstacle_trajectory_intersects_obstacle"
                            )
                            is True
                        ),
                        "obstacle_trajectory_geometry_status": (
                            immediate_clearance_observation.get(
                                "obstacle_trajectory_geometry_status"
                            )
                        ),
                    },
                    "response": (
                        "bounded_action_observed"
                        if recovery_action_completion_verified
                        else "bounded_action_failed"
                    ),
                    "observed_at": recovery_goal_observed_at,
                    "approval_created_by_proposal": False,
                    "automatic_redispatch_performed": False,
                    "physical_execution_invoked": False,
                }
            )
            recovery_checkpoint = {
                **recovery_checkpoint,
                "claimed_at": str(
                    recovery_approval.get("approved_at")
                    or dispatched_at.isoformat()
                ),
                "claimed_by_approval_ref": recovery_approval[
                    "operator_approval_ref"
                ],
                "checkpoint_status": (
                    "consumed" if recovery_action_completion_verified else "failed"
                ),
                (
                    "consumed_at" if recovery_action_completion_verified else "failed_at"
                ): recovery_goal_observed_at,
            }
            if recovery_action_completion_verified:
                recovery_checkpoint["consumed_by_approval_ref"] = (
                    recovery_approval["operator_approval_ref"]
                )
            if not recovery_action_completion_verified:
                recovery_failure_reasons = list(
                    recovery_segment_result.get("blocking_reasons")
                    or ["turtlebot3_recovery_goal_not_completed"]
                )
                if (
                    recovery_goal_sequence_completed
                    and immediate_clearance_verification_required
                    and not immediate_clearance_verification_satisfied
                ):
                    recovery_failure_reasons = [
                        "obstacle_trajectory_intersects_obstacle"
                        if immediate_clearance_observation.get(
                            "obstacle_trajectory_intersects_obstacle"
                        )
                        is True
                        else "obstacle_trajectory_clearance_not_observed"
                    ]
                if (
                    recovery_goal_sequence_completed
                    and immediate_side_verification_required
                    and not immediate_side_verification_satisfied
                ):
                    recovery_failure_reasons = [
                        "requested_recovery_side_not_observed_in_raw_map_frame"
                    ]
                recovery_checkpoint["failure_reasons"] = list(
                    recovery_failure_reasons
                )
                recovery_repair_parent_checkpoint = dict(recovery_checkpoint)
                (
                    recovery_checkpoint,
                    runtime_recovery_obstacle_scenario,
                    recovery_planner_result,
                    recovery_proposals,
                    recovery_proposal_classifications,
                    runtime_recovery_motion_context,
                ) = _build_recovery_failure_followup_checkpoint(
                    parent_checkpoint=recovery_checkpoint,
                    proposal=proposal,
                    goals=goals,
                    segment_results=segment_results,
                    approved_recovery_results=(
                        approved_recovery_segment_results
                    ),
                    route_failure_observation_results=(
                        route_failure_observation_results
                    ),
                    recovery_closed_loop_cycles=recovery_closed_loop_cycles,
                    autonomy_envelope=autonomy_envelope,
                    battery_envelope=battery_envelope,
                )
                recovery_action_suggested = str(
                    recovery_checkpoint.get("selected_action") or ""
                )
                runtime_recovery_action_kind = recovery_action_suggested
                recovery_candidate_resolution = dict(
                    runtime_recovery_obstacle_scenario.get(
                        "recovery_candidate_resolution"
                    )
                    or {}
                )
        if (
            not resume_requested
            or (
                route_resumed_after_recovery
            )
        ):
            segment_indexes = range(start_segment_index, len(goals) + 1)
        else:
            segment_indexes = range(0)
        for index in segment_indexes:
            segment_goal = goals[index - 1]
            previous_predicate_evaluation = (
                segment_results[-1].get(
                    "mission_contract_predicate_evaluation"
                )
                if index > 1 and segment_results
                else None
            )
            transition_authority = (
                evaluate_turtlebot3_segment_transition_authority(
                    binding=route_authority,
                    proposal_id=str(
                        proposal.get("proposal_id")
                        or "turtlebot3_home_mission"
                    ),
                    operator_approval_ref=approval_ref,
                    approved_scope=str(approval.get("approved_scope") or ""),
                    planned_segments=goals,
                    autonomy_envelope=autonomy_envelope,
                    segment_index=index,
                    segment_ref=f"segment_{index}",
                    goal=segment_goal,
                    previous_predicate_evaluation=(
                        previous_predicate_evaluation
                        if isinstance(
                            previous_predicate_evaluation,
                            Mapping,
                        )
                        else None
                    ),
                )
            )
            segment_transition_authority_records.append(
                transition_authority
            )
            if transition_authority["transition_status"] != "authorized":
                blocking_reasons.extend(
                    transition_authority["blocking_reasons"]
                )
                config = Ros2Nav2HardwareAdapterConfig(
                    missionos_action_ref=(
                        f"{proposal.get('proposal_id') or 'turtlebot3_home_mission'}:"
                        f"segment_{index}"
                    ),
                    goal_pose=segment_goal,
                    execution_mode=HardwareExecutionMode.SIM,
                    operator_approval_ref=approval_ref or None,
                    approval_actor=str(
                        approval.get("approval_actor")
                        or "missionos_chat_operator"
                    ),
                    approval_timestamp=dispatched_at,
                    max_distance_m=segment_goal.max_distance_m,
                    raw_logs_ref=_turtlebot3_raw_logs_ref_from_env(
                        robot_profile
                    ),
                )
                evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
                    config=config,
                    blocking_reasons=tuple(
                        transition_authority["blocking_reasons"]
                    ),
                )
                break
            simulate_post_recovery_failure = (
                _truthy_env(
                    TURTLEBOT3_SIMULATE_POST_RECOVERY_ROUTE_FAILURE_ONCE_ENV
                )
                and resume_requested
                and route_resumed_after_recovery
                and not route_failure_observation_results
                and index == start_segment_index
            )
            result = _dispatch_nav2_goal(
                proposal=proposal,
                approval=approval,
                goal=segment_goal,
                approval_ref=approval_ref,
                dispatched_at=dispatched_at,
                action_ref_suffix=f"segment_{index}",
                publish_initialpose=index == 1,
                simulate_cancel_after_accept=simulate_post_recovery_failure,
            )
            result["segment_transition_authority"] = dict(
                transition_authority
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
                    harness_stop_dispatcher=lambda reflex: _dispatch_harness_stop(
                        reflex=reflex,
                        proposal=proposal,
                    ),
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
                if recovery_action_suggested != "avoid_obstacle":
                    # The prior obstacle candidate belongs to the consumed
                    # checkpoint. A new decision must not display or bind that
                    # stale candidate as if it validated the fresh observation.
                    recovery_candidate_resolution = {}
                    runtime_recovery_obstacle_scenario = {
                        key: value
                        for key, value in runtime_recovery_obstacle_scenario.items()
                        if key != "recovery_candidate_resolution"
                    }
                elif (
                    result.get("obstacle_detected") is True
                    or result.get("costmap_obstacle_observed") is True
                ):
                    runtime_recovery_obstacle_scenario = (
                        _runtime_recovery_obstacle_scenario(
                            proposal.get("obstacle_scenario")
                            if isinstance(proposal.get("obstacle_scenario"), Mapping)
                            else {},
                            segment_result=result,
                        )
                    )
                    parent_binding = recovery_checkpoint.get(
                        "recovery_candidate_binding"
                    )
                    parent_binding = (
                        parent_binding
                        if isinstance(parent_binding, Mapping)
                        else {}
                    )
                    excluded_candidate_ids = {
                        str(candidate_id)
                        for candidate_id in (
                            parent_binding.get("candidate_ids")
                            or [parent_binding.get("candidate_id")]
                        )
                        if str(candidate_id or "")
                    }
                    recovery_candidate_resolution = _resolve_recovery_candidate(
                        runtime_recovery_obstacle_scenario,
                        segment_results=[
                            *segment_results,
                            *route_failure_observation_results,
                        ],
                        excluded_candidate_ids=excluded_candidate_ids,
                    )
                    runtime_recovery_obstacle_scenario = {
                        **runtime_recovery_obstacle_scenario,
                        "recovery_candidate_resolution": dict(
                            recovery_candidate_resolution
                        ),
                    }
                    recovery_planner_result = {
                        **recovery_planner_result,
                        "recovery_candidate_resolution": dict(
                            recovery_candidate_resolution
                        ),
                    }
                recovery_execution_permitted_by_envelope = any(
                    classification.get("execution_permitted_by_envelope") is True
                    for classification in recovery_proposal_classifications
                )
                if (
                    recovery_action_suggested == "return_home"
                    and recovery_execution_permitted_by_envelope
                ):
                    followup_recovery_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=_profile_home_pose(),
                        approval_ref=approval_ref,
                        dispatched_at=dispatched_at,
                        action_ref_suffix="recovery_return_home_after_failure",
                        publish_initialpose=False,
                    )
                    if resume_requested and recovery_segment_result:
                        subsequent_recovery_segment_results.append(
                            followup_recovery_result
                        )
                    else:
                        recovery_segment_result = followup_recovery_result
                    evidence = followup_recovery_result["adapter_evidence"]
                elif (
                    recovery_proposal_classifications
                    and recovery_proposal_classifications[0].get(
                        "execution_class"
                    )
                    == "requires_human_approval"
                    and recovery_proposal_classifications[0].get(
                        "requires_new_human_approval"
                    )
                    is True
                ):
                    followup_parent = (
                        dict(recovery_checkpoint)
                        if resume_requested
                        and recovery_checkpoint.get("checkpoint_status")
                        == "consumed"
                        else {}
                    )
                    failed_observation = dict(result)
                    route_failure_observation_results.append(failed_observation)
                    if segment_results and segment_results[-1] is result:
                        segment_results.pop()
                    followup_checkpoint = _build_turtlebot3_recovery_checkpoint(
                        proposal=proposal,
                        goals=goals,
                        segment_results=segment_results,
                        recovery_proposals=recovery_proposals,
                        recovery_proposal_classifications=(
                            recovery_proposal_classifications
                        ),
                        recovery_planner_result=recovery_planner_result,
                        runtime_recovery_obstacle_scenario=(
                            runtime_recovery_obstacle_scenario
                        ),
                        runtime_recovery_motion_context=(
                            runtime_recovery_motion_context
                        ),
                        completed_segment_index=index - 1,
                        route_failure_observation_results=(
                            route_failure_observation_results
                        ),
                    )
                    followup_checkpoint = {
                        **followup_checkpoint,
                        "followup_trigger": (
                            "route_segment_failed_after_verified_recovery"
                        ),
                        "followup_failed_segment_index": index,
                        "followup_failure_observation_sha256": hashlib.sha256(
                            json.dumps(
                                failed_observation,
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "operator_guidance_required": (
                            recovery_action_suggested
                            in {"ask_human", "hold", "safe_stop"}
                        ),
                        "requires_new_human_approval": True,
                        "automatic_redispatch_performed": False,
                        "prior_closed_loop_cycle_refs": [
                            {
                                "cycle_index": cycle.get("cycle_index"),
                                "checkpoint_id": cycle.get("checkpoint_id"),
                                "reobservation_sha256": cycle.get(
                                    "reobservation_sha256"
                                ),
                            }
                            for cycle in recovery_closed_loop_cycles
                        ],
                    }
                    if followup_parent:
                        recovery_followup_parent_checkpoint = followup_parent
                        followup_checkpoint = {
                            **followup_checkpoint,
                            "parent_checkpoint_id": followup_parent.get(
                                "checkpoint_id"
                            ),
                            "parent_checkpoint_hash": followup_parent.get(
                                "checkpoint_hash"
                            ),
                        }
                    followup_hash = _recovery_checkpoint_hash(followup_checkpoint)
                    followup_checkpoint["checkpoint_hash"] = followup_hash
                    followup_checkpoint["checkpoint_id"] = (
                        "turtlebot3_recovery_checkpoint_"
                        f"{followup_hash[:12]}"
                    )
                    recovery_checkpoint = followup_checkpoint
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
                    harness_stop_dispatcher=lambda reflex: _dispatch_harness_stop(
                        reflex=reflex,
                        proposal=proposal,
                    ),
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
                    followup_recovery_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=_profile_home_pose(),
                        approval_ref=approval_ref,
                        dispatched_at=dispatched_at,
                        action_ref_suffix="recovery_return_home",
                        publish_initialpose=False,
                    )
                    if resume_requested and recovery_segment_result:
                        subsequent_recovery_segment_results.append(
                            followup_recovery_result
                        )
                    else:
                        recovery_segment_result = followup_recovery_result
                    evidence = followup_recovery_result["adapter_evidence"]
                    _emit_progress(
                        runtime_recovery_triggered=True,
                        recovery_action_suggested=runtime_recovery_action_kind,
                    )
                elif (
                    recovery_action_suggested == "return_home"
                    and recovery_proposal_classifications
                    and recovery_proposal_classifications[0].get(
                        "execution_class"
                    )
                    == "requires_human_approval"
                    and recovery_proposal_classifications[0].get(
                        "requires_new_human_approval"
                    )
                    is True
                ):
                    recovery_checkpoint = _build_turtlebot3_recovery_checkpoint(
                        proposal=proposal,
                        goals=goals,
                        segment_results=segment_results,
                        recovery_proposals=recovery_proposals,
                        recovery_proposal_classifications=(
                            recovery_proposal_classifications
                        ),
                        recovery_planner_result=recovery_planner_result,
                        runtime_recovery_obstacle_scenario=(
                            runtime_recovery_obstacle_scenario
                        ),
                        runtime_recovery_motion_context=(
                            runtime_recovery_motion_context
                        ),
                        completed_segment_index=index,
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
                    camera_observation_payload,
                    camera_perception_pipeline,
                ) = _capture_camera_perception_observation(
                    decision_epoch_ref=(
                        f"{proposal.get('proposal_id') or 'turtlebot3_home_mission'}:"
                        f"segment:{index}:perception"
                    )
                )
                perception_claim_responses = tuple(
                    result.get("bridge_responses") or ()
                )
                if camera_observation_payload:
                    perception_claim_responses = (
                        *perception_claim_responses,
                        {"camera_observation": camera_observation_payload},
                    )
                runtime_recovery_perception_claims = (
                    build_perception_claims_from_env_or_responses(
                        perception_claim_responses,
                        costmap_obstacle_observed=(
                            runtime_recovery_obstacle_scenario.get(
                                "costmap_obstacle_observed"
                            )
                            is True
                        ),
                        observed_at=None,
                        runtime_context=camera_perception_pipeline,
                    )
                )
                runtime_recovery_obstacle_scenario["perception_claims"] = [
                    claim.model_dump(mode="json")
                    for claim in runtime_recovery_perception_claims
                ]
                runtime_recovery_obstacle_scenario[
                    "camera_perception_pipeline"
                ] = dict(camera_perception_pipeline)
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
                    harness_stop_dispatcher=lambda reflex: _dispatch_harness_stop(
                        reflex=reflex,
                        proposal=proposal,
                    ),
                    perception_claims=runtime_recovery_perception_claims,
                )
                recovery_proposals = tuple(
                    item.model_dump(mode="json")
                    for item in runtime_recovery_proposals
                )
                recovery_planner_result = dict(runtime_recovery_planner_result)
                recovery_candidate_resolution = _resolve_recovery_candidate(
                    runtime_recovery_obstacle_scenario,
                    segment_results=segment_results,
                )
                runtime_recovery_obstacle_scenario = {
                    **runtime_recovery_obstacle_scenario,
                    "recovery_candidate_resolution": dict(
                        recovery_candidate_resolution
                    ),
                }
                recovery_planner_result = {
                    **recovery_planner_result,
                    "recovery_candidate_resolution": dict(
                        recovery_candidate_resolution
                    ),
                }
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
                recovery_dispatch_authority_source = (
                    "autonomy_envelope"
                    if recovery_execution_permitted_by_envelope
                    else "fresh_operator_approval"
                    if recovery_execution_permitted_by_operator_approval
                    else None
                )
                if (
                    recovery_action_suggested == "avoid_obstacle"
                    and recovery_candidate_resolution.get("resolution_status")
                    != "blocked"
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
                    selected_candidate = recovery_candidate_resolution.get(
                        "selected_candidate"
                    )
                    selected_candidate = (
                        selected_candidate
                        if isinstance(selected_candidate, Mapping)
                        else {}
                    )
                    resolved_recovery_goal = (
                        _profile_dynamic_obstacle_avoidance_goal().model_copy(
                            update={
                                "x_m": float(selected_candidate["x_m"]),
                                "y_m": float(selected_candidate["y_m"]),
                            }
                        )
                        if selected_candidate
                        else _profile_dynamic_obstacle_avoidance_goal()
                    )
                    recovery_segment_result = _dispatch_nav2_goal(
                        proposal=proposal,
                        approval=approval,
                        goal=resolved_recovery_goal,
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
                    classification = (
                        recovery_proposal_classifications[0]
                        if recovery_proposal_classifications
                        else {}
                    )
                    if (
                        recovery_action_suggested == "avoid_obstacle"
                        and recovery_candidate_resolution.get("resolution_status")
                        != "blocked"
                        and classification.get("execution_class")
                        == "requires_human_approval"
                        and classification.get("requires_new_human_approval") is True
                    ):
                        recovery_checkpoint = _build_turtlebot3_recovery_checkpoint(
                            proposal=proposal,
                            goals=goals,
                            segment_results=segment_results,
                            recovery_proposals=recovery_proposals,
                            recovery_proposal_classifications=(
                                recovery_proposal_classifications
                            ),
                            recovery_planner_result=recovery_planner_result,
                            runtime_recovery_obstacle_scenario=(
                                runtime_recovery_obstacle_scenario
                            ),
                            runtime_recovery_motion_context=(
                                runtime_recovery_motion_context
                            ),
                            completed_segment_index=index,
                        )
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

    if runtime_recovery_battery_envelope and runtime_recovery_home_distance_envelope:
        runtime_recovery_battery_envelope = {
            **runtime_recovery_battery_envelope,
            "battery_return_decision": _battery_return_decision(
                battery_envelope=runtime_recovery_battery_envelope,
                home_distance_envelope=runtime_recovery_home_distance_envelope,
            ),
        }

    evidence_payload = (
        evidence.model_dump(mode="json")
        if hasattr(evidence, "model_dump")
        else dict(evidence)
    )
    current_approved_recovery_results = (
        approved_recovery_segment_results
        if approved_recovery_segment_results
        else ([recovery_segment_result] if recovery_segment_result else [])
    )
    recorded_approved_recovery_results = [
        *prior_recovery_segment_results,
        *current_approved_recovery_results,
    ]
    if not recovery_requested_side_observation:
        recovery_requested_side_observation = (
            _recovery_requested_side_observation(
                checkpoint=recovery_checkpoint,
                approved_recovery_results=current_approved_recovery_results,
            )
        )
    requested_side_verification_required = (
        recovery_requested_side_observation.get("observation_status")
        != "not_required"
    )
    requested_side_verification_satisfied = (
        not requested_side_verification_required
        or recovery_requested_side_observation.get("requested_side_observed") is True
    )
    all_action_results = [
        *segment_results,
        *route_failure_observation_results,
        *recorded_approved_recovery_results,
        *subsequent_recovery_segment_results,
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
    main_dispatch_sent = any(
        result.get("dispatch_request_sent") is True
        for result in [
            *segment_results,
            *route_failure_observation_results,
        ]
    )
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
        and route_resumed_after_recovery
    )
    recovery_dispatch_request_sent = any(
        result.get("dispatch_request_sent") is True
        for result in current_approved_recovery_results
    )
    recovery_goal_sequence_completed = bool(current_approved_recovery_results) and all(
        result.get("completion_claimed") is True
        for result in current_approved_recovery_results
    )
    recovery_completion_claimed = (
        recovery_goal_sequence_completed and requested_side_verification_satisfied
    )
    subsequent_recovery_dispatch_request_sent = any(
        result.get("dispatch_request_sent") is True
        for result in subsequent_recovery_segment_results
    )
    subsequent_recovery_completion_claimed = any(
        result.get("completion_claimed") is True
        for result in subsequent_recovery_segment_results
    )
    recovery_outcome_completion_claimed = (
        subsequent_recovery_completion_claimed
        if subsequent_recovery_segment_results
        else recovery_completion_claimed
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
    _recovery_camera_pipeline = (
        runtime_recovery_obstacle_scenario.get("camera_perception_pipeline")
        if isinstance(
            runtime_recovery_obstacle_scenario.get("camera_perception_pipeline"),
            Mapping,
        )
        else {}
    )
    _recovery_sensor_observation = (
        (_recovery_camera_pipeline.get("capture") or {}).get(
            "camera_lidar_observation"
        )
        if isinstance(_recovery_camera_pipeline.get("capture"), Mapping)
        else {}
    )
    visual_observations = build_visual_observations(
        claims=runtime_recovery_obstacle_scenario.get("perception_claims") or (),
        sensor_observation=_recovery_sensor_observation or {},
    )
    obstacle_geometry = _obstacle_trajectory_geometry(
        obstacle_required=obstacle_required,
        obstacle=obstacle,
        observed_points=[
            point
            for result in [
                *segment_results[pre_recovery_segment_result_count:],
                *route_failure_observation_results,
            ]
            for point in _observed_points_from_action_result(result)
        ],
        recovery_points=[
            point
            for result in current_approved_recovery_results
            for point in _observed_points_from_action_result(result)
        ],
        visual_observations=visual_observations,
        robot_profile=_robot_profile_from_proposal(proposal),
    )
    bridge_obstacle_avoidance_observed = obstacle["obstacle_avoidance_observed"]
    obstacle.update(obstacle_geometry)
    obstacle["bridge_obstacle_avoidance_observed"] = bridge_obstacle_avoidance_observed
    obstacle["requested_side_verification_required"] = (
        requested_side_verification_required
    )
    obstacle["requested_side_observed"] = (
        recovery_requested_side_observation.get("requested_side_observed") is True
    )
    robot_profile = _robot_profile_from_proposal(proposal)
    footprint_3d_clearance_required = robot_profile == "turtlebot3"
    if obstacle_required:
        obstacle["obstacle_avoidance_observed"] = (
            bridge_obstacle_avoidance_observed is True
            and obstacle_geometry["obstacle_trajectory_clearance_observed"] is True
            and (
                not footprint_3d_clearance_required
                or obstacle_geometry[
                    "obstacle_trajectory_3d_clearance_observed"
                ]
                is True
            )
        )
    delivery_route_requested = mission_kind == "indoor_delivery_route_leg"
    obstacle_avoidance_completion_claimed = (
        (main_segments_completed or route_completed_after_recovery)
        and obstacle_required
        and obstacle["obstacle_avoidance_observed"] is True
        and requested_side_verification_satisfied
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
    verified_closed_loop_cycles = [
        cycle
        for cycle in recovery_closed_loop_cycles
        if cycle.get("dispatch_request_sent") is True
        and cycle.get("action_materialized") is True
        and cycle.get("reobservation_status") == "verified"
        and bool(cycle.get("reobservation_sha256"))
    ]
    observed_closed_loop_cycles = [
        cycle
        for cycle in recovery_closed_loop_cycles
        if cycle.get("dispatch_request_sent") is True
        and cycle.get("action_materialized") is True
        and cycle.get("reobservation_status") in {"verified", "failed"}
        and bool(cycle.get("reobservation_sha256"))
    ]
    distinct_cycle_checkpoint_ids = {
        str(cycle.get("checkpoint_id") or "")
        for cycle in observed_closed_loop_cycles
        if str(cycle.get("checkpoint_id") or "")
    }
    distinct_cycle_approval_refs = {
        str(cycle.get("operator_approval_ref") or "")
        for cycle in observed_closed_loop_cycles
        if str(cycle.get("operator_approval_ref") or "")
    }
    form3_closed_loop_claimed = (
        mission_completion_claimed
        and len(observed_closed_loop_cycles) >= 2
        and len(distinct_cycle_checkpoint_ids) >= 2
        and len(distinct_cycle_approval_refs) >= 2
    )
    form3_closed_loop_status = (
        "verified" if form3_closed_loop_claimed else "not_verified"
    )
    mission_blocking_reasons = list(evidence_payload.get("blocking_reasons") or [])
    mission_blocking_reasons.extend(
        str(reason)
        for reason in nvblox_evidence_payload.get("blocking_reasons") or []
    )
    if recovery_candidate_resolution.get("resolution_status") == "blocked":
        candidate_reasons = list(
            recovery_candidate_resolution.get("blocking_reasons") or []
        )
        mission_blocking_reasons.extend(
            candidate_reasons or ["nav2_recovery_candidate_validation_unavailable"]
        )
    if (
        recovery_goal_sequence_completed
        and requested_side_verification_required
        and not requested_side_verification_satisfied
    ):
        mission_blocking_reasons.append(
            "requested_recovery_side_not_observed_in_raw_map_frame"
        )
    if (
        route_completion_candidate
        and obstacle_required
        and not obstacle_avoidance_completion_claimed
    ):
        mission_blocking_reasons.append("obstacle_avoidance_not_observed")
        if obstacle.get("obstacle_trajectory_intersects_obstacle") is True:
            mission_blocking_reasons.append("obstacle_trajectory_intersects_obstacle")
        if obstacle.get("obstacle_trajectory_3d_collision_observed") is True:
            mission_blocking_reasons.append(
                "robot_collision_envelope_intersects_obstacle_volume"
            )
        elif (
            footprint_3d_clearance_required
            and obstacle.get("obstacle_trajectory_3d_clearance_status")
            == "unavailable"
        ):
            mission_blocking_reasons.append(
                "obstacle_trajectory_3d_clearance_evidence_unavailable"
            )
        if obstacle.get("obstacle_trajectory_geometry_status") in {
            "raw_map_frame_trajectory_unavailable",
            "raw_map_frame_trajectory_insufficient",
        }:
            mission_blocking_reasons.append(
                "obstacle_trajectory_raw_map_frame_evidence_unavailable"
            )
        if (
            requested_side_verification_required
            and not requested_side_verification_satisfied
        ):
            mission_blocking_reasons.append(
                "requested_recovery_side_not_observed_in_raw_map_frame"
            )
    if route_completion_candidate and telemetry_sidecar_required:
        if not telemetry_sidecar_motion_confirmed:
            mission_blocking_reasons.append(
                "telemetry_sidecar_motion_correlation_not_confirmed"
            )
        mission_blocking_reasons.extend(telemetry_sidecar_blocking_reasons)
    status = (
        "completed"
        if mission_completion_claimed
        else "pending"
        if (
            recovery_checkpoint.get("checkpoint_status")
            == "awaiting_operator_approval"
            and (
                not blocking_reasons
                or bool(runtime_failure_context)
                or bool(recovery_repair_parent_checkpoint)
                or bool(recovery_followup_parent_checkpoint)
            )
        )
        else "recovered"
        if runtime_recovery_triggered and recovery_outcome_completion_claimed
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
        segment_results=[
            *segment_results,
            *route_failure_observation_results,
        ],
        recovery_segment_result=recovery_segment_result,
        approved_recovery_segment_results=recorded_approved_recovery_results,
        subsequent_recovery_segment_results=subsequent_recovery_segment_results,
        status=status,
        obstacle_required=obstacle_required,
        obstacle=obstacle,
        motion=motion,
        runtime_recovery_triggered=runtime_recovery_triggered,
        recovery_action_suggested=recovery_action_suggested,
        route_resumed_after_recovery=route_resumed_after_recovery,
        visual_observations=visual_observations,
    )
    indoor_map_model["recovery"]["goal_sequence_completed"] = (
        recovery_goal_sequence_completed
    )
    indoor_map_model["recovery"]["completion_claimed"] = (
        recovery_completion_claimed
    )
    recovery_status = _recovery_runtime_status_projection()
    indoor_map_model["recovery"].update(
        {
            "goal_status": recovery_status["recovery_goal_status"],
            "goal_succeeded_observed": recovery_status[
                "recovery_goal_succeeded_observed"
            ],
            "verification_status": recovery_status[
                "recovery_verification_status"
            ],
            "route_resume_status": recovery_status["route_resume_status"],
        }
    )
    indoor_map_model["recovery"]["requested_side_observation"] = dict(
        recovery_requested_side_observation
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
        "route_authority": dict(route_authority or {}),
        "segment_transition_authority_records": [
            dict(item) for item in segment_transition_authority_records
        ],
        "route_failure_observation_results": [
            dict(item) for item in route_failure_observation_results
        ],
        "adapter_evidence": dict(evidence_payload),
        "adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {}) for item in segment_results
        ],
        "route_failure_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in route_failure_observation_results
        ],
        "recovery_segment_result": dict(recovery_segment_result),
        "approved_recovery_segment_results": [
            dict(item) for item in recorded_approved_recovery_results
        ],
        "recovery_attempt_history": [
            dict(item) for item in recorded_approved_recovery_results
        ],
        "recovery_closed_loop_cycles": [
            dict(item) for item in recovery_closed_loop_cycles
        ],
        "recovery_closed_loop_verified_cycle_count": len(
            verified_closed_loop_cycles
        ),
        "recovery_closed_loop_observed_cycle_count": len(
            observed_closed_loop_cycles
        ),
        "form3_closed_loop_status": form3_closed_loop_status,
        "form3_closed_loop_claimed": form3_closed_loop_claimed,
        "subsequent_recovery_segment_results": [
            dict(item) for item in subsequent_recovery_segment_results
        ],
        "recovery_adapter_evidence": dict(
            recovery_segment_result.get("adapter_evidence") or {}
        ),
        "approved_recovery_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in recorded_approved_recovery_results
        ],
        "subsequent_recovery_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in subsequent_recovery_segment_results
        ],
        "latest_adapter_evidence_role": (
            "subsequent_recovery"
            if subsequent_recovery_segment_results
            else "approved_recovery"
            if recovery_segment_result
            else "route_segment"
        ),
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
        "recovery_candidate_resolution": dict(recovery_candidate_resolution),
        "recovery_candidate_revalidation": dict(
            recovery_candidate_revalidation
        ),
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
        "recovery_requested_side_observation": dict(
            recovery_requested_side_observation
        ),
        "recovery_execution_permitted_by_envelope": (
            recovery_execution_permitted_by_envelope
        ),
        "recovery_execution_permitted_by_operator_approval": (
            recovery_execution_permitted_by_operator_approval
        ),
        "recovery_dispatch_authority_source": recovery_dispatch_authority_source,
        "runtime_recovery_triggered": runtime_recovery_triggered,
        **recovery_status,
        "runtime_recovery_action_kind": runtime_recovery_action_kind,
        "turtlebot3_recovery_checkpoint": dict(recovery_checkpoint),
        "turtlebot3_recovery_repair_parent_checkpoint": dict(
            recovery_repair_parent_checkpoint
        ),
        "turtlebot3_recovery_followup_parent_checkpoint": dict(
            recovery_followup_parent_checkpoint
        ),
        "runtime_failure_context": dict(runtime_failure_context),
        "runtime_failure_recovery_triggered": bool(runtime_failure_context),
        "route_resumed_after_recovery": route_resumed_after_recovery,
        "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
        "recovery_goal_sequence_completed": recovery_goal_sequence_completed,
        "recovery_completion_claimed": recovery_completion_claimed,
        "subsequent_recovery_dispatch_request_sent": (
            subsequent_recovery_dispatch_request_sent
        ),
        "subsequent_recovery_completion_claimed": (
            subsequent_recovery_completion_claimed
        ),
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
            1
            for result in [
                *segment_results,
                *route_failure_observation_results,
            ]
            if result.get("dispatch_request_sent") is True
        ),
        "segment_completion_count": sum(
            1 for result in segment_results if result.get("completion_claimed") is True
        ),
        "segment_transition_authority_count": len(
            segment_transition_authority_records
        ),
        "segment_transition_authorized_count": sum(
            1
            for item in segment_transition_authority_records
            if item.get("transition_status") == "authorized"
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
        "turtlebot3_recovery_checkpoint": dict(recovery_checkpoint),
        "turtlebot3_recovery_repair_parent_checkpoint": dict(
            recovery_repair_parent_checkpoint
        ),
        "turtlebot3_recovery_followup_parent_checkpoint": dict(
            recovery_followup_parent_checkpoint
        ),
        "turtlebot3_indoor_map_model": dict(indoor_map_model),
        "log_bundle_artifacts": dict(log_bundle_artifacts),
        "telemetry_sidecar_artifacts": dict(telemetry_sidecar_artifacts),
        "nvblox_perception_evidence": dict(nvblox_evidence_payload),
        "recovery_candidate_resolution": dict(recovery_candidate_resolution),
        "recovery_candidate_revalidation": dict(
            recovery_candidate_revalidation
        ),
        "ros2_nav2_hardware_adapter_evidence": dict(evidence_payload),
        "ros2_nav2_hardware_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {}) for item in segment_results
        ],
        "ros2_nav2_route_failure_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in route_failure_observation_results
        ],
        "ros2_nav2_recovery_adapter_evidence": dict(
            recovery_segment_result.get("adapter_evidence") or {}
        ),
        "ros2_nav2_approved_recovery_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in recorded_approved_recovery_results
        ],
        "ros2_nav2_subsequent_recovery_adapter_evidence_segments": [
            dict(item.get("adapter_evidence") or {})
            for item in subsequent_recovery_segment_results
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
            "recovery_candidate_resolution": dict(recovery_candidate_resolution),
            "recovery_candidate_revalidation": dict(
                recovery_candidate_revalidation
            ),
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
            "recovery_requested_side_observation": dict(
                recovery_requested_side_observation
            ),
            "recovery_execution_permitted_by_envelope": (
                recovery_execution_permitted_by_envelope
            ),
            "recovery_execution_permitted_by_operator_approval": (
                recovery_execution_permitted_by_operator_approval
            ),
            "recovery_dispatch_authority_source": recovery_dispatch_authority_source,
            "runtime_recovery_triggered": runtime_recovery_triggered,
            **recovery_status,
            "runtime_recovery_action_kind": runtime_recovery_action_kind,
            "turtlebot3_recovery_checkpoint": dict(recovery_checkpoint),
            "turtlebot3_recovery_repair_parent_checkpoint": dict(
                recovery_repair_parent_checkpoint
            ),
            "turtlebot3_recovery_followup_parent_checkpoint": dict(
                recovery_followup_parent_checkpoint
            ),
            "runtime_failure_context": dict(runtime_failure_context),
            "runtime_failure_recovery_triggered": bool(runtime_failure_context),
            "route_resumed_after_recovery": route_resumed_after_recovery,
            "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
            "recovery_goal_sequence_completed": recovery_goal_sequence_completed,
            "recovery_completion_claimed": recovery_completion_claimed,
            "subsequent_recovery_dispatch_request_sent": (
                subsequent_recovery_dispatch_request_sent
            ),
            "subsequent_recovery_completion_claimed": (
                subsequent_recovery_completion_claimed
            ),
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
            "route_authority": dict(route_authority or {}),
            "segment_transition_authority_records": [
                dict(item)
                for item in segment_transition_authority_records
            ],
            "segment_transition_authority_count": execution[
                "segment_transition_authority_count"
            ],
            "segment_transition_authorized_count": execution[
                "segment_transition_authorized_count"
            ],
            "route_failure_observation_results": [
                dict(item) for item in route_failure_observation_results
            ],
            "recovery_segment_result": dict(recovery_segment_result),
            "approved_recovery_segment_results": [
                dict(item) for item in recorded_approved_recovery_results
            ],
            "recovery_attempt_history": [
                dict(item) for item in recorded_approved_recovery_results
            ],
            "recovery_closed_loop_cycles": [
                dict(item) for item in recovery_closed_loop_cycles
            ],
            "recovery_closed_loop_verified_cycle_count": len(
                verified_closed_loop_cycles
            ),
            "recovery_closed_loop_observed_cycle_count": len(
                observed_closed_loop_cycles
            ),
            "form3_closed_loop_status": form3_closed_loop_status,
            "form3_closed_loop_claimed": form3_closed_loop_claimed,
            "subsequent_recovery_segment_results": [
                dict(item) for item in subsequent_recovery_segment_results
            ],
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
            "obstacle_trajectory_geometry_status": obstacle[
                "obstacle_trajectory_geometry_status"
            ],
            "obstacle_trajectory_geometry_frame_id": obstacle[
                "obstacle_trajectory_geometry_frame_id"
            ],
            "obstacle_trajectory_raw_map_frame_sample_count": obstacle[
                "obstacle_trajectory_raw_map_frame_sample_count"
            ],
            "obstacle_trajectory_observed_stream_count": obstacle[
                "obstacle_trajectory_observed_stream_count"
            ],
            "obstacle_trajectory_observed_segment_count": obstacle[
                "obstacle_trajectory_observed_segment_count"
            ],
            "obstacle_trajectory_non_map_sample_count_excluded": obstacle[
                "obstacle_trajectory_non_map_sample_count_excluded"
            ],
            "obstacle_trajectory_ineligible_map_sample_count_excluded": obstacle[
                "obstacle_trajectory_ineligible_map_sample_count_excluded"
            ],
            "obstacle_trajectory_path_sample_count_excluded": obstacle[
                "obstacle_trajectory_path_sample_count_excluded"
            ],
            "obstacle_trajectory_display_aligned_sample_count_excluded": obstacle[
                "obstacle_trajectory_display_aligned_sample_count_excluded"
            ],
            "obstacle_trajectory_invalid_numeric_sample_count_excluded": obstacle[
                "obstacle_trajectory_invalid_numeric_sample_count_excluded"
            ],
            "obstacle_trajectory_display_alignment_used": obstacle[
                "obstacle_trajectory_display_alignment_used"
            ],
            "obstacle_trajectory_3d_clearance": obstacle.get(
                "obstacle_trajectory_3d_clearance"
            ),
            "obstacle_trajectory_3d_clearance_observed": obstacle.get(
                "obstacle_trajectory_3d_clearance_observed"
            )
            is True,
            "obstacle_trajectory_3d_collision_observed": obstacle.get(
                "obstacle_trajectory_3d_collision_observed"
            )
            is True,
            "obstacle_trajectory_3d_clearance_status": obstacle.get(
                "obstacle_trajectory_3d_clearance_status"
            ),
            "obstacle_trajectory_geometry_claim_boundary": obstacle.get(
                "obstacle_trajectory_geometry_claim_boundary"
            ),
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
    "TURTLEBOT3_RECOVERY_CHECKPOINT_SCHEMA",
    "TURTLEBOT3_RECOVERY_CHECKPOINT_REVISION_SCHEMA",
    "TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_SCHEMA",
    "TurtleBot3MissionJudgmentPoint",
    "TurtleBot3HomeMissionPlan",
    "approve_turtlebot3_home_mission_plan",
    "build_turtlebot3_home_mission_plan",
    "build_turtlebot3_recovery_checkpoint_revision",
    "infer_turtlebot3_home_mission_kind",
    "infer_turtlebot_home_robot_profile",
    "instruction_requests_turtlebot3_home_mission",
    "normalize_turtlebot_nav2_robot_profile",
    "run_turtlebot3_home_mission_dispatch",
]
