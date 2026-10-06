"""Closed model-site declarations and independent geometry checks, no flight."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_return_sites as sites
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import _attitude


def inputs():
    config = json.loads(Path("examples/spaceflight/starship-return-sites-model-test.json").read_text())
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    return config, profile, catch


def catalog():
    config, profile, catch = inputs()
    return sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)


def test_fixture_binds_original_profile_and_declared_30km_parallel_without_real_site_claims():
    config, profile, catch = inputs()
    before = deepcopy((config, profile, catch))
    result = sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)
    assert result.capture.latitude_deg == profile["launch"]["latitude_deg"]
    assert result.capture.longitude_deg == profile["launch"]["longitude_deg"]
    latitude = math.radians(result.capture.latitude_deg)
    prime = 6378137./math.sqrt(1-(1/298.257223563)*(2-1/298.257223563)*math.sin(latitude)**2)
    distance = math.radians(result.divert.longitude_deg-result.capture.longitude_deg)*prime*math.cos(latitude)
    assert distance == pytest.approx(30000., abs=1e-7)
    assert result.divert.support_points_body_m == ()
    assert result.divert.terminal_goal == "model_surface_contact"
    assert result.to_dict() == config
    assert result.to_dict()["safe_landing_area_verified"] is False
    assert result.to_dict()["surveyed_mission_coordinates"] is False
    assert (config, profile, catch) == before


def test_catalog_objects_and_nested_support_points_are_immutable_and_copies_are_isolated():
    config, profile, catch = inputs()
    result = sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)
    checksum = result.sha256
    config["sites"][0]["support_points_body_m"][0][0] = 999.
    copied = result.to_dict()
    copied["sites"][1]["longitude_deg"] = 0.
    assert result.sha256 == checksum
    with pytest.raises(FrozenInstanceError):
        result.divert.longitude_deg = 0.
    with pytest.raises(TypeError):
        result.capture.support_points_body_m[0][0] = 0.
    with pytest.raises(sites.ReturnSiteError, match="immutable"):
        replace(result.capture, support_points_body_m=[[-6., 0., 60.], [6., 0., 60.]])


@pytest.mark.parametrize("key,value", [("synthetic_model_sites", False), ("surveyed_mission_coordinates", True),
    ("spacex_clearance_verified", True), ("safe_landing_area_verified", True), ("wave_or_water_contact_model", True),
    ("coordinate_datum", "mean_sea_level"), ("nominal_east_distance_m", 200.), ("extra", "live_spacecraft")])
def test_catalog_rejects_changed_distance_extra_fields_or_operational_claims(key, value):
    config, profile, catch = inputs()
    config[key] = value
    with pytest.raises(sites.ReturnSiteError):
        sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)


@pytest.mark.parametrize("field,value", [("latitude_deg", math.nan), ("longitude_deg", True),
    ("elevation_m", 1.), ("surface_footprint_radius_m", 10001.), ("maximum_contact_ground_speed_mps", 3.1),
    ("maximum_contact_vertical_speed_mps", 2.1), ("maximum_contact_tilt_deg", 5.1),
    ("maximum_contact_body_rate_rad_s", .021), ("support_height_m", 100.), ("support_points_body_m", [[0., 0., 1.]]),
    ("unknown_callback", "forecast")])
def test_divert_contact_bounds_remain_closed_and_cannot_become_a_second_tower(field, value):
    config, profile, catch = inputs()
    config["sites"][1][field] = value
    with pytest.raises(sites.ReturnSiteError):
        sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)


def test_capture_binding_and_exact_two_site_ids_reject_replaced_goals():
    for change in ("capture", "divert", "duplicate", "extra"):
        config, profile, catch = inputs()
        if change == "capture":
            config["sites"][0]["latitude_deg"] += .001
        elif change == "divert":
            config["sites"][1]["longitude_deg"] += .001
        elif change == "duplicate":
            config["sites"][1] = deepcopy(config["sites"][0])
        else:
            config["sites"].append(deepcopy(config["sites"][1]))
        with pytest.raises(sites.ReturnSiteError):
            sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)


@pytest.mark.parametrize("time_s", [0., 600., 3600.])
def test_target_frame_matches_existing_wgs84_rotation_and_has_orthonormal_axes(time_s):
    site = catalog().divert
    frame = sites.return_site_frame(site, time_s)
    reference = env.surface_state(site.latitude_deg, site.longitude_deg, time_s=time_s)
    assert frame["origin_eci_m"] == pytest.approx(reference.r, abs=1e-7)
    assert frame["origin_velocity_eci_mps"] == pytest.approx(reference.v, abs=1e-9)
    axes = [frame[key] for key in ("east_eci", "north_eci", "up_eci")]
    assert all(math.hypot(*axis) == pytest.approx(1.) for axis in axes)
    assert all(sum(a*b for a, b in zip(axes[i], axes[j])) == pytest.approx(0., abs=1e-12)
               for i, j in ((0, 1), (0, 2), (1, 2)))
    assert env.cross(axes[0], axes[1]) == pytest.approx(axes[2], abs=1e-12)


def test_surface_state_error_uses_ground_relative_velocity_and_does_not_assign_state():
    site = catalog().divert
    point = env.surface_state(site.latitude_deg, site.longitude_deg, time_s=600.)
    up, east, _ = env.local_frame(point)
    state = {"time_s": 600., "r_eci_m": list(point.r), "v_eci_mps": list(point.v),
        "q_body_to_eci": list(_attitude(up, east)), "omega_body_rad_s": [0., 0., 0.]}
    before = deepcopy(state)
    result = sites.state_errors(site, state)
    assert result["position_error_enu_m"] == pytest.approx([0., 0., 0.], abs=1e-7)
    assert result["ground_velocity_enu_mps"] == pytest.approx([0., 0., 0.], abs=1e-9)
    assert result["position_error_frame"] == "target_site_tangent_enu_not_geodetic_height"
    assert result["contact_or_support_verified"] is False
    assert result["state_assigned"] is False
    assert state == before


def test_capture_pins_require_actual_centroid_and_remove_ground_rotation_at_material_points():
    site = catalog().capture
    point = env.surface_state(site.latitude_deg, site.longitude_deg, 75.4, time_s=600.)
    up, east, _ = env.local_frame(point)
    quaternion = _attitude(up, east)
    state = {"time_s": 600., "r_eci_m": list(point.r), "v_eci_mps": list(point.v),
        "q_body_to_eci": list(quaternion), "omega_body_rad_s": list(dyn.inverse_rotate(quaternion, (0., 0., env.EARTH_ROTATION_RAD_S)))}
    with pytest.raises(sites.ReturnSiteError, match="measured_centroid"):
        sites.state_errors(site, state)
    result = sites.state_errors(site, state, com_body_m=[0., 0., 32.], com_rate_body_mps=[0., 0., 0.])
    pins = result["surrogate_pins"]
    assert [pin["height_above_support_m"] for pin in pins] == pytest.approx([3.4, 3.4], abs=1e-7)
    assert [pin["position_enu_m"][0] for pin in pins] == pytest.approx([-6., 6.], abs=1e-7)
    assert all(pin["velocity_enu_mps"] == pytest.approx([0., 0., 0.], abs=1e-8) for pin in pins)


def test_near_unit_quaternion_geometry_normalizes_without_changing_the_saved_input():
    site = catalog().divert
    point = env.surface_state(site.latitude_deg, site.longitude_deg, time_s=600.)
    up, east, _ = env.local_frame(point)
    exact = {"time_s": 600., "r_eci_m": list(point.r), "v_eci_mps": list(point.v),
        "q_body_to_eci": list(_attitude(up, east)), "omega_body_rad_s": [0., 0., 0.]}
    scaled = {**exact, "q_body_to_eci": [x*(1+4e-9) for x in exact["q_body_to_eci"]]}
    before = deepcopy(scaled)
    assert sites.state_errors(site, scaled)["tilt_deg"] == pytest.approx(sites.state_errors(site, exact)["tilt_deg"], abs=1e-5)
    assert scaled == before
    with pytest.raises(sites.ReturnSiteError):
        sites.state_errors(site, {**exact, "q_body_to_eci": [0., 0., 0., 0.]})
