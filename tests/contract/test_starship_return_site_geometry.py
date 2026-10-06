"""Static target/material geometry only; no integration or flight forecasts."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_return_sites as sites
from src.runtime.starship_booster_catch import contact_frame
from src.runtime.starship_booster_recovery import _tower_observation, surface_return_observation
from src.runtime.starship_sixdof_booster import _navigation
from src.runtime.starship_sixdof_mission import _attitude, point_state, vehicle
from src.runtime.starship_terminal_guidance import surface_terminal_geometry, terminal_horizontal_request


@pytest.fixture(autouse=True)
def no_integration(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No physical integration is admitted by this geometry test.")
    monkeypatch.setattr(dyn, "step", forbidden)
    monkeypatch.setattr(env, "step", forbidden)


@pytest.fixture
def inputs():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    catalog = sites.ReturnSites.from_dict(json.loads(Path("examples/spaceflight/starship-return-sites-model-test.json").read_text()),
        profile=profile, catch_config=catch)
    return profile, catch, catalog


def static_state(profile, site, *, time_s=600., altitude_m=300., firing=False):
    body = vehicle(profile, "booster")
    point = env.surface_state(site.latitude_deg, site.longitude_deg, altitude_m, time_s=time_s)
    up, east, north = env.local_frame(point)
    quaternion = _attitude(up, east)
    omega = dyn.inverse_rotate(quaternion, (0., 0., env.EARTH_ROTATION_RAD_S))
    state = dyn.State6DOF(time_s, point.r, env.add(point.v, env.add(env.scale(east, 12.),
        env.add(env.scale(north, -7.), env.scale(up, -3.)))), quaternion,
        env.add(omega, (.004, -.006, .002)), 78000.,
        tuple(dyn.EngineState(throttle=.5 if firing and i < 3 else 0.) for i in range(len(body.engines))),
        tuple(0. for _ in body.aero_panels))
    return body, state


def legacy_navigation(state, profile):
    """Frozen pre-site numerical formula, independent of the new branches."""
    point = point_state(state)
    up, east, north = env.local_frame(point)
    target = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=state.time_s)
    relative = env.air_relative_velocity(point)
    delta = env.add(target.r, env.scale(state.r_eci_m, -1.))
    displacement = env.add(delta, env.scale(up, -env.dot(delta, up)))
    a, b = env.unit(state.r_eci_m), env.unit(target.r)
    distance = env.EARTH_EQUATORIAL_RADIUS_M*math.atan2(env.norm(env.cross(a, b)), env.dot(a, b))
    return up, east, north, relative, displacement, distance


@pytest.mark.parametrize("time_s", [0., 153.9, 600., 3600.])
def test_default_none_and_explicit_capture_preserve_exact_legacy_arrays(inputs, time_s):
    profile, catch, catalog = inputs
    body, state = static_state(profile, catalog.capture, time_s=time_s, firing=True)
    original = deepcopy((profile, catch, asdict(state)))
    legacy = legacy_navigation(state, profile)
    assert _navigation(state, profile) == legacy
    assert _navigation(state, profile, return_site=None) == legacy
    assert _navigation(state, profile, return_site=catalog.capture) == legacy
    implicit = contact_frame(state, body, profile, catch, time_s-.1, authorized=True, support_armed=(True, True))
    explicit = contact_frame(state, body, profile, catch, time_s-.1, authorized=True,
        support_armed=(True, True), return_site=catalog.capture)
    assert implicit == explicit
    arrival = _tower_observation(state, body, profile, catch)
    assert arrival == _tower_observation(state, body, profile, catch, return_site=None)
    assert arrival == _tower_observation(state, body, profile, catch, return_site=catalog.capture)
    implicit_request = terminal_horizontal_request(state, arrival, profile, catch, 9.8)
    assert implicit_request == terminal_horizontal_request(state, arrival, profile, catch, 9.8, return_site=catalog.capture)
    assert (profile, catch, asdict(state)) == original


@pytest.mark.parametrize("time_s", [0., 600., 3600.])
def test_divert_changes_only_target_and_keeps_current_ground_frame(inputs, time_s):
    profile, _, catalog = inputs
    _, state = static_state(profile, catalog.capture, time_s=time_s)
    original = deepcopy((profile, asdict(state)))
    legacy = _navigation(state, profile)
    alternate = _navigation(state, profile, return_site=catalog.divert)
    assert alternate[:4] == legacy[:4]
    target = sites.return_site_frame(catalog.divert, time_s)["origin_eci_m"]
    delta = tuple(target[i]-state.r_eci_m[i] for i in range(3))
    expected = env.add(delta, env.scale(alternate[0], -env.dot(delta, alternate[0])))
    assert alternate[4] == pytest.approx(expected, abs=1e-7)
    assert 29900. < env.norm(alternate[4]) < 30100.
    assert alternate[4] != legacy[4] and alternate[5] > legacy[5]
    assert (profile, asdict(state)) == original


def test_ground_relative_navigation_is_not_wind_or_site_origin_relative(inputs):
    profile, _, catalog = inputs
    _, state = static_state(profile, catalog.divert)
    up, east, north, ground, _, _ = _navigation(state, profile, return_site=catalog.divert)
    assert [env.dot(ground, axis) for axis in (east, north, up)] == pytest.approx([12., -7., -3.], abs=1e-8)
    origin_velocity = sites.return_site_frame(catalog.divert, state.time_s)["origin_velocity_eci_mps"]
    wrong = tuple(state.v_eci_mps[i]-origin_velocity[i] for i in range(3))
    assert env.norm(env.add(ground, env.scale(wrong, -1.))) > .01


@pytest.mark.parametrize("change", ["latitude", "longitude", "elevation", "support_height", "support_points", "clearance"])
def test_capture_rejects_rebound_geometry_instead_of_moving_original_profile(inputs, change):
    profile, catch, catalog = inputs
    body, state = static_state(profile, catalog.capture)
    changed = {"latitude": dict(latitude_deg=catalog.capture.latitude_deg+.01),
        "longitude": dict(longitude_deg=catalog.capture.longitude_deg+.01), "elevation": dict(elevation_m=1.),
        "support_height": dict(support_height_m=101.), "support_points": dict(support_points_body_m=((-5., 0., 60.), (5., 0., 60.))),
        "clearance": dict(pin_clearance_center_m=7.)}[change]
    site = replace(catalog.capture, **changed)
    with pytest.raises(sites.ReturnSiteError):
        contact_frame(state, body, profile, catch, state.time_s, return_site=site)
    with pytest.raises(sites.ReturnSiteError):
        _tower_observation(state, body, profile, catch, return_site=site)
    if change in ("latitude", "longitude", "elevation"):
        with pytest.raises(sites.ReturnSiteError):
            _navigation(state, profile, return_site=site)


def test_divert_and_mutable_lookalike_cannot_enter_pin_support_helpers(inputs):
    profile, catch, catalog = inputs
    body, state = static_state(profile, catalog.capture)
    arrival = _tower_observation(state, body, profile, catch)
    for site in (catalog.divert, catalog.capture.to_dict(), SimpleNamespace(**catalog.capture.to_dict())):
        with pytest.raises(sites.ReturnSiteError):
            contact_frame(state, body, profile, catch, state.time_s, return_site=site)
        with pytest.raises(sites.ReturnSiteError):
            _tower_observation(state, body, profile, catch, return_site=site)
        with pytest.raises(sites.ReturnSiteError):
            terminal_horizontal_request(state, arrival, profile, catch, 9.8, return_site=site)
    with pytest.raises(sites.ReturnSiteError):
        surface_terminal_geometry(state, profile, return_site=catalog.capture)
    with pytest.raises(sites.ReturnSiteError):
        surface_return_observation(state, body, profile, return_site=catalog.capture)


@pytest.mark.parametrize("time_s", [0., 600., 3600.])
def test_surface_target_axes_epoch_and_cg_state_have_no_tower_pins(inputs, time_s):
    profile, _, catalog = inputs
    body, state = static_state(profile, catalog.divert, time_s=time_s, firing=True)
    original = deepcopy((profile, asdict(state)))
    geometry = surface_terminal_geometry(state, profile, return_site=catalog.divert)
    observation = surface_return_observation(state, body, profile, return_site=catalog.divert)
    frame = geometry["target_frame"]
    assert frame == sites.return_site_frame(catalog.divert, time_s)
    assert geometry["position_error_enu_m"] == pytest.approx([0., 0., 300.], abs=1e-7)
    assert geometry["ground_velocity_enu_mps"] == pytest.approx([12., -7., -3.], abs=1e-7)
    assert geometry["observed_body_axis_enu"] == pytest.approx([0., 0., 1.], abs=1e-12)
    assert geometry["earth_rotation_removed"] is True
    assert geometry["position_error_frame"] == "target_site_tangent_enu_not_geodetic_height"
    assert observation["terminal_geometry"] == geometry
    assert observation["return_site_sha256"] == catalog.divert.sha256
    assert not {"pins", "eligible", "arm_half_span_m", "normal_force_n"} & set(observation)
    assert all(observation[key] is False for key in ("state_assigned", "capture_allowed", "contact_or_support_verified", "safe_landing_verified"))
    assert (profile, asdict(state)) == original


def quaternion_multiply(a, b):
    w, x, y, z = a
    u, v, s, t = b
    return (w*u-x*v-y*s-z*t, w*v+x*u+y*t-z*s, w*s-x*t+y*u+z*v, w*t+x*s-y*v+z*u)


def rotate_reference(q, vector):
    w, x, y, z = q
    matrix = ((1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)),
        (2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)),
        (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)))
    return tuple(sum(a*b for a, b in zip(row, vector)) for row in matrix)


def test_material_point_velocity_includes_depletion_centroid_motion_and_earth_rotation(inputs):
    profile, _, catalog = inputs
    body, state = static_state(profile, catalog.divert, firing=True)
    observation = surface_return_observation(state, body, profile, return_site=catalog.divert)
    assert env.norm(observation["com_rate_body_mps"]) > 1e-6
    point = observation["hull_geometry"]["point_body_m"]
    com, com_rate = observation["com_body_m"], observation["com_rate_body_mps"]
    quaternion = observation["terminal_geometry"]["geometry_q_body_to_eci"]
    omega = state.omega_body_rad_s
    magnitude = math.hypot(*omega)
    def local_position(delta):
        angle = magnitude*delta/2.
        increment = (math.cos(angle), *(component*math.sin(angle)/magnitude for component in omega))
        q = quaternion_multiply(quaternion, increment)
        lever = tuple(point[i]-com[i]-com_rate[i]*delta for i in range(3))
        rotated = rotate_reference(q, lever)
        world = tuple(state.r_eci_m[i]+state.v_eci_mps[i]*delta+rotated[i] for i in range(3))
        frame = sites.return_site_frame(catalog.divert, state.time_s+delta)
        displacement = tuple(world[i]-frame["origin_eci_m"][i] for i in range(3))
        return tuple(sum(a*b for a, b in zip(displacement, frame[key])) for key in ("east_eci", "north_eci", "up_eci"))
    interval = .01
    before, after = local_position(-interval), local_position(interval)
    derivative = [(b-a)/(2*interval) for a, b in zip(before, after)]
    assert observation["material_point_ground_velocity_enu_mps"] == pytest.approx(derivative, abs=3e-6)
    assert observation["material_point_ground_speed_mps"] == pytest.approx(math.hypot(*derivative), abs=3e-6)


def test_tilt_rate_uses_rotating_target_axes_and_actual_body_rate(inputs):
    profile, _, catalog = inputs
    _, state = static_state(profile, catalog.divert)
    geometry = surface_terminal_geometry(state, profile, return_site=catalog.divert)
    assert geometry["observed_tilt_rate_enu_rad_s"] == pytest.approx([-.006, -.004], abs=1e-12)
    earth_only = replace(state, omega_body_rad_s=dyn.inverse_rotate(state.q_body_to_eci, (0., 0., env.EARTH_ROTATION_RAD_S)))
    assert surface_terminal_geometry(earth_only, profile, return_site=catalog.divert)["observed_tilt_rate_enu_rad_s"] == pytest.approx([0., 0.], abs=1e-12)


def test_target_tangent_height_is_not_a_fake_surface_or_tower_support_height(inputs):
    profile, _, catalog = inputs
    body, state = static_state(profile, catalog.capture, altitude_m=300.)
    geometry = surface_terminal_geometry(state, profile, return_site=catalog.divert)
    observation = surface_return_observation(state, body, profile, return_site=catalog.divert)
    assert 220. < geometry["position_error_enu_m"][2] < 235.
    assert observation["hull_geometry"]["signed_clearance_m"] > 200.
    assert observation["hull_geometry"]["clearance_measure"] == "minimum_ellipsoid_level_set_radius_difference"
    assert abs(observation["hull_geometry"]["signed_clearance_m"]-geometry["position_error_enu_m"][2]) > 1.


def test_surface_geometry_normalizes_near_unit_roundoff_without_assigning_saved_state(inputs):
    profile, _, catalog = inputs
    body, exact = static_state(profile, catalog.divert)
    scaled = replace(exact, q_body_to_eci=tuple(x*(1+4e-9) for x in exact.q_body_to_eci))
    before = asdict(scaled)
    expected = surface_return_observation(exact, body, profile, return_site=catalog.divert)
    actual = surface_return_observation(scaled, body, profile, return_site=catalog.divert)
    assert actual["hull_geometry"] == expected["hull_geometry"]
    assert actual["terminal_geometry"]["observed_body_axis_enu"] == expected["terminal_geometry"]["observed_body_axis_enu"]
    assert actual["material_point_ground_velocity_enu_mps"] == expected["material_point_ground_velocity_enu_mps"]
    assert actual["state_sha256"] != expected["state_sha256"] and asdict(scaled) == before
    with pytest.raises(ValueError):
        surface_terminal_geometry(replace(exact, q_body_to_eci=(0., 0., 0., 0.)), profile, return_site=catalog.divert)


def test_divert_cannot_be_replaced_by_an_arbitrary_unapproved_surface_location(inputs):
    profile, _, catalog = inputs
    body, state = static_state(profile, catalog.divert)
    changed = replace(catalog.divert, longitude_deg=catalog.divert.longitude_deg+.001)
    with pytest.raises(sites.ReturnSiteError, match="declared_30000m"):
        _navigation(state, profile, return_site=changed)
    with pytest.raises(sites.ReturnSiteError, match="declared_30000m"):
        surface_return_observation(state, body, profile, return_site=changed)
