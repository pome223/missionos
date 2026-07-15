from __future__ import annotations

import xml.etree.ElementTree as ET

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import world


def _fragment_root(fragment: str) -> ET.Element:
    return ET.fromstring(f"<sdf><world>{fragment}</world></sdf>")


def test_legacy_route_entrypoint_uses_packaged_world_builders() -> None:
    bindings = {
        "_alternate_landing_world_sdf_patch": world.alternate_landing_world_sdf_patch,
        "_inject_visibility_fog_render_marker": world.inject_visibility_fog_render_marker,
        "_landing_zone_blocked_world_sdf_patch": world.landing_zone_blocked_world_sdf_patch,
        "_moving_actor_waypoint_motion_spec": world.moving_actor_waypoint_motion_spec,
        "_moving_actor_waypoint_trajectory_definition_sha256": world.moving_actor_waypoint_trajectory_definition_sha256,
        "_moving_actor_world_sdf_patch": world.moving_actor_world_sdf_patch,
        "_no_fly_zone_world_sdf_patch": world.no_fly_zone_world_sdf_patch,
        "_payload_model_sdf_patch": world.payload_model_sdf_patch,
        "_payload_world_sdf_patch": world.payload_world_sdf_patch,
        "_traffic_conflict_world_sdf_patch": world.traffic_conflict_world_sdf_patch,
        "_visibility_marker_fog_element": world.visibility_marker_fog_element,
        "_wind_effects_world_sdf_patch": world.wind_effects_world_sdf_patch,
    }
    for legacy_name, packaged_builder in bindings.items():
        assert getattr(route_entrypoint, legacy_name) is packaged_builder


def test_world_model_fragments_are_valid_xml_with_stable_identities() -> None:
    fragments = {
        "delivery_payload": world.payload_world_sdf_patch(payload_mass_kg=2.5),
        "mission_designer_landing_zone_blocked_marker": (
            world.landing_zone_blocked_world_sdf_patch()
        ),
        "mission_designer_no_fly_zone_marker": world.no_fly_zone_world_sdf_patch(),
        "mission_designer_traffic_conflict_marker": (
            world.traffic_conflict_world_sdf_patch()
        ),
        "mission_designer_alternate_landing_marker": (
            world.alternate_landing_world_sdf_patch()
        ),
        "mission_designer_moving_actor_marker": world.moving_actor_world_sdf_patch(),
    }

    for expected_model_name, fragment in fragments.items():
        model = _fragment_root(fragment).find(".//model")
        assert model is not None
        assert model.get("name") == expected_model_name

    payload = _fragment_root(fragments["delivery_payload"])
    assert payload.findtext(".//mass") == "2.500000"


def test_vehicle_plugin_and_wind_fragments_remain_valid_xml() -> None:
    payload_plugin = _fragment_root(world.payload_model_sdf_patch())
    detachable_joint = payload_plugin.find(".//plugin")
    assert detachable_joint is not None
    assert detachable_joint.get("filename") == "gz-sim-detachable-joint-system"
    assert detachable_joint.findtext("detach_topic") == (
        "/model/x500_0/delivery_payload/detach"
    )

    wind = _fragment_root(
        world.wind_effects_world_sdf_patch(wind_x_mps=3.25, wind_y_mps=-1.5)
    )
    assert wind.findtext(".//wind/linear_velocity") == "3.25 -1.5 0"
    plugin_names = {plugin.get("filename") for plugin in wind.findall(".//plugin")}
    assert "gz-sim-wind-effects-system" in plugin_names
    assert "gz-sim-physics-system" in plugin_names


def test_fog_injection_is_idempotent_and_readable() -> None:
    original = "<sdf><world><scene></scene></world></sdf>"
    injected = world.inject_visibility_fog_render_marker(original)
    reinjected = world.inject_visibility_fog_render_marker(injected)

    assert reinjected == injected
    assert injected.count(world.VISIBILITY_FOG_RENDER_MARKER_ID) == 1
    fog = world.visibility_marker_fog_element(injected)
    assert fog is not None
    assert fog.findtext("type") == world.VISIBILITY_FOG_RENDER_TYPE
    assert fog.findtext("color") == world.VISIBILITY_FOG_RENDER_COLOR
    assert fog.findtext("density") == world.VISIBILITY_FOG_RENDER_DENSITY
    assert fog.findtext("start") == world.VISIBILITY_FOG_RENDER_START_M
    assert fog.findtext("end") == world.VISIBILITY_FOG_RENDER_END_M


def test_moving_actor_motion_contract_and_hash_are_deterministic() -> None:
    assert world.moving_actor_waypoint_motion_spec() == {
        "mode": "linear_waypoint_motion",
        "actor_id": "mission_designer_moving_actor_marker",
        "frame": "gazebo_world_local",
        "start_xy_m": [1.2, -0.7],
        "end_xy_m": [4.2, 3.2],
        "loop_seconds": 6.0,
        "nominal_profile_velocity_mps": 0.8200609733428363,
    }
    assert world.moving_actor_waypoint_trajectory_definition_sha256() == (
        "d343e94f06c467bc0a406f343ddf2c7ac8b8ad8ddf0f819f6c9ac26dbb96c2a6"
    )
