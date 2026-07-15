"""Pure Gazebo world and SDF construction for the PX4 route runtime.

This module only builds or inspects text and XML. It does not mutate files,
start a simulator, dispatch commands, or create execution authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any
import xml.etree.ElementTree as ET

def payload_model_sdf_patch() -> str:
    return """
    <plugin filename="gz-sim-detachable-joint-system"
            name="gz::sim::systems::DetachableJoint">
      <parent_link>base_link</parent_link>
      <child_model>delivery_payload</child_model>
      <child_link>payload_link</child_link>
      <detach_topic>/model/x500_0/delivery_payload/detach</detach_topic>
      <attach_topic>/model/x500_0/delivery_payload/attach</attach_topic>
      <output_topic>/model/x500_0/delivery_payload/state</output_topic>
    </plugin>
"""


def payload_world_sdf_patch(*, payload_mass_kg: float) -> str:
    return f"""
    <model name="delivery_payload">
      <pose>0 0 0.04 0 0 0</pose>
      <static>false</static>
      <link name="payload_link">
        <inertial>
          <mass>{payload_mass_kg:.6f}</mass>
          <inertia>
            <ixx>0.0001</ixx><ixy>0</ixy><ixz>0</ixz>
            <iyy>0.0001</iyy><iyz>0</iyz><izz>0.0001</izz>
          </inertia>
        </inertial>
        <collision name="payload_collision">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
        </collision>
        <visual name="payload_visual">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
          <material><diffuse>0.1 0.5 1.0 1</diffuse></material>
        </visual>
      </link>
    </model>
"""


def landing_zone_blocked_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_landing_zone_blocked_marker">
      <pose>5.0 5.0 0.025 0 0 0</pose>
      <static>true</static>
      <link name="marker_link">
        <visual name="blocked_marker_visual">
          <geometry><box><size>0.9 0.9 0.05</size></box></geometry>
          <material><diffuse>1.0 0.12 0.08 0.65</diffuse></material>
        </visual>
      </link>
    </model>
"""


VISIBILITY_FOG_RENDER_MARKER_ID = "mission_designer_visibility_fog_render_marker"
VISIBILITY_FOG_RENDER_TYPE = "linear"
VISIBILITY_FOG_RENDER_DENSITY = "0.35"
VISIBILITY_FOG_RENDER_COLOR = "0.72 0.76 0.78 1"
VISIBILITY_FOG_RENDER_START_M = "0.0"
VISIBILITY_FOG_RENDER_END_M = "25.0"


def visibility_fog_render_marker_sdf_patch() -> str:
    return f"""
      <!-- {VISIBILITY_FOG_RENDER_MARKER_ID} -->
      <fog>
        <type>{VISIBILITY_FOG_RENDER_TYPE}</type>
        <color>{VISIBILITY_FOG_RENDER_COLOR}</color>
        <density>{VISIBILITY_FOG_RENDER_DENSITY}</density>
        <start>{VISIBILITY_FOG_RENDER_START_M}</start>
        <end>{VISIBILITY_FOG_RENDER_END_M}</end>
      </fog>
"""


def inject_visibility_fog_render_marker(world_text: str) -> str:
    if VISIBILITY_FOG_RENDER_MARKER_ID in world_text:
        return world_text
    fog_patch = visibility_fog_render_marker_sdf_patch()
    if "</scene>" in world_text:
        return world_text.replace("</scene>", fog_patch + "    </scene>", 1)
    return world_text.replace(
        "  </world>\n</sdf>",
        f"    <scene>{fog_patch}    </scene>\n  </world>\n</sdf>",
    )


def visibility_marker_fog_element(world_text: str) -> ET.Element | None:
    marker_index = world_text.find(VISIBILITY_FOG_RENDER_MARKER_ID)
    if marker_index < 0:
        return None
    fog_start = world_text.find("<fog", marker_index)
    if fog_start < 0:
        return None
    scene_end = world_text.find("</scene>", marker_index)
    if scene_end >= 0 and fog_start > scene_end:
        return None
    fog_end = world_text.find("</fog>", fog_start)
    if fog_end < 0:
        return None
    fog_fragment = world_text[fog_start : fog_end + len("</fog>")]
    try:
        return ET.fromstring(fog_fragment)
    except ET.ParseError:
        return None


def no_fly_zone_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_no_fly_zone_marker">
      <pose>2.5 2.5 1.0 0 0 0</pose>
      <static>true</static>
      <link name="no_fly_zone_marker_link">
        <visual name="no_fly_zone_marker_visual">
          <geometry><cylinder><radius>1.25</radius><length>2.0</length></cylinder></geometry>
          <material><diffuse>1.0 0.0 0.0 0.22</diffuse></material>
          <transparency>0.78</transparency>
        </visual>
      </link>
    </model>
"""


def traffic_conflict_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_traffic_conflict_marker">
      <pose>3.6 2.9 0.25 0 0 0.785398</pose>
      <static>true</static>
      <link name="traffic_conflict_marker_link">
        <visual name="traffic_conflict_marker_visual">
          <geometry><box><size>0.8 0.35 0.5</size></box></geometry>
          <material><diffuse>1.0 0.62 0.0 0.48</diffuse></material>
          <transparency>0.52</transparency>
        </visual>
      </link>
    </model>
"""


def alternate_landing_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_alternate_landing_marker">
      <pose>-2.0 3.5 0.03 0 0 0</pose>
      <static>true</static>
      <link name="alternate_landing_marker_link">
        <visual name="alternate_landing_marker_visual">
          <geometry><cylinder><radius>0.65</radius><length>0.06</length></cylinder></geometry>
          <material><diffuse>0.1 0.72 1.0 0.42</diffuse></material>
          <transparency>0.58</transparency>
        </visual>
      </link>
    </model>
"""


def moving_actor_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_moving_actor_marker">
      <pose>1.2 -0.7 0.25 0 0 0</pose>
      <link name="moving_actor_marker_link">
        <gravity>false</gravity>
        <inertial>
          <mass>1.0</mass>
          <inertia>
            <ixx>0.1</ixx>
            <iyy>0.1</iyy>
            <izz>0.1</izz>
          </inertia>
        </inertial>
        <visual name="moving_actor_marker_visual">
          <geometry><box><size>0.35 0.35 0.5</size></box></geometry>
          <material><diffuse>0.95 0.15 0.65 0.58</diffuse></material>
          <transparency>0.42</transparency>
        </visual>
      </link>
      <plugin filename="gz-sim-trajectory-follower-system"
              name="gz::sim::systems::TrajectoryFollower">
        <link_name>moving_actor_marker_link</link_name>
        <loop>true</loop>
        <force>10</force>
        <torque>10</torque>
        <waypoints>
          <waypoint>1.2 -0.7</waypoint>
          <waypoint>4.2 3.2</waypoint>
        </waypoints>
      </plugin>
    </model>
"""


def moving_actor_waypoint_motion_spec() -> dict[str, Any]:
    start_xy = [1.2, -0.7]
    end_xy = [4.2, 3.2]
    loop_seconds = 6.0
    distance_m = math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
    return {
        "mode": "linear_waypoint_motion",
        "actor_id": "mission_designer_moving_actor_marker",
        "frame": "gazebo_world_local",
        "start_xy_m": start_xy,
        "end_xy_m": end_xy,
        "loop_seconds": loop_seconds,
        "nominal_profile_velocity_mps": distance_m / loop_seconds,
    }


def moving_actor_waypoint_trajectory_definition_sha256() -> str:
    return hashlib.sha256(
        json.dumps(
            moving_actor_waypoint_motion_spec(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()



def wind_effects_world_sdf_patch(*, wind_x_mps: float, wind_y_mps: float) -> str:
    return f"""
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">
    </plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands">
    </plugin>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster">
    </plugin>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu">
    </plugin>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure">
    </plugin>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed">
    </plugin>
    <wind>
      <linear_velocity>{wind_x_mps} {wind_y_mps} 0</linear_velocity>
    </wind>
    <plugin filename="gz-sim-apply-link-wrench-system" name="gz::sim::systems::ApplyLinkWrench">
    </plugin>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat">
    </plugin>
    <plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer">
    </plugin>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <plugin filename="libOpticalFlowSystem.so" name="custom::OpticalFlowSystem">
    </plugin>
    <plugin filename="libGstCameraSystem.so" name="custom::GstCameraSystem">
    </plugin>
    <plugin filename="gz-sim-wind-effects-system" name="gz::sim::systems::WindEffects">
      <force_approximation_scaling_factor>1</force_approximation_scaling_factor>
      <horizontal>
        <magnitude>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude_percent>0.0</amplitude_percent>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </magnitude>
        <direction>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude>0</amplitude>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </direction>
      </horizontal>
      <vertical>
        <noise type="gaussian">
          <mean>0</mean>
          <stddev>0</stddev>
        </noise>
      </vertical>
    </plugin>
"""

__all__ = [
    "VISIBILITY_FOG_RENDER_COLOR",
    "VISIBILITY_FOG_RENDER_DENSITY",
    "VISIBILITY_FOG_RENDER_END_M",
    "VISIBILITY_FOG_RENDER_MARKER_ID",
    "VISIBILITY_FOG_RENDER_START_M",
    "VISIBILITY_FOG_RENDER_TYPE",
    "alternate_landing_world_sdf_patch",
    "inject_visibility_fog_render_marker",
    "landing_zone_blocked_world_sdf_patch",
    "moving_actor_waypoint_motion_spec",
    "moving_actor_waypoint_trajectory_definition_sha256",
    "moving_actor_world_sdf_patch",
    "no_fly_zone_world_sdf_patch",
    "payload_model_sdf_patch",
    "payload_world_sdf_patch",
    "traffic_conflict_world_sdf_patch",
    "visibility_fog_render_marker_sdf_patch",
    "visibility_marker_fog_element",
    "wind_effects_world_sdf_patch",
]
