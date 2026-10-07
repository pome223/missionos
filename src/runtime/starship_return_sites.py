"""Immutable model return-site declarations and WGS84 geometry, no guidance.

The east site is a synthetic test target, not surveyed mission coordinates,
SpaceX clearance, a safe landing area or an ocean/wave model. Selecting a site
cannot move a vehicle. A controller and independent trajectory checks remain
separate. The launch profile is never changed by these helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math

SCHEMA = "missionos.starship_return_sites.v1"
SITE_SCHEMA = "missionos.starship_return_site.v1"
WGS84_A_M = 6_378_137.
WGS84_F = 1/298.257223563
WGS84_E2 = WGS84_F*(2-WGS84_F)
EARTH_ROTATION_RAD_S = 7.292115e-5
EAST_DISTANCE_M = 30_000.
DERIVATION = "same_geodetic_latitude_wgs84_parallel_arc_30000m_east"
_CATALOG_FIELDS = {"schema", "profile_id", "coordinate_datum", "derivation", "nominal_east_distance_m", "sites",
                   "synthetic_model_sites", "surveyed_mission_coordinates", "spacex_clearance_verified",
                   "safe_landing_area_verified", "wave_or_water_contact_model"}
_SITE_FIELDS = {"schema", "site_id", "latitude_deg", "longitude_deg", "elevation_m", "terminal_goal",
                "surface_footprint_radius_m", "maximum_contact_ground_speed_mps", "maximum_contact_vertical_speed_mps",
                "maximum_contact_tilt_deg", "maximum_contact_body_rate_rad_s", "minimum_contact_propellant_kg",
                "support_height_m", "pin_clearance_center_m", "support_points_body_m"}


class ReturnSiteError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise ReturnSiteError(reason)


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _vector(value, length=3):
    return type(value) in (list, tuple) and len(value) == length and all(_number(x, -1e12, 1e12) for x in value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _copy(value):
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ReturnSiteError("invalid_return_site_json") from None


def east_model_coordinates(latitude_deg, longitude_deg):
    """A declared parallel arc, not a geodesic or surveyed operational location."""
    _require(_number(latitude_deg, -80., 80.) and _number(longitude_deg, -180., 180.), "invalid_model_origin")
    latitude = math.radians(latitude_deg)
    prime = WGS84_A_M/math.sqrt(1-WGS84_E2*math.sin(latitude)**2)
    longitude = longitude_deg+math.degrees(EAST_DISTANCE_M/(prime*math.cos(latitude)))
    longitude = (longitude+180.) % 360.-180.
    return float(latitude_deg), longitude


@dataclass(frozen=True)
class ReturnSite:
    site_id: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    terminal_goal: str
    surface_footprint_radius_m: float
    maximum_contact_ground_speed_mps: float
    maximum_contact_vertical_speed_mps: float
    maximum_contact_tilt_deg: float
    maximum_contact_body_rate_rad_s: float
    minimum_contact_propellant_kg: float
    support_height_m: float
    pin_clearance_center_m: float
    support_points_body_m: tuple[tuple[float, float, float], ...]

    def __post_init__(self):
        _require(type(self.site_id) is str and self.site_id in ("capture", "divert")
            and type(self.terminal_goal) is str and type(self.support_points_body_m) is tuple
            and all(type(point) is tuple and _vector(point) for point in self.support_points_body_m),
            "return_site_must_be_closed_and_immutable")
        for name, low, high in (("latitude_deg", -80., 80.), ("longitude_deg", -180., 180.), ("elevation_m", -100., 5000.),
            ("surface_footprint_radius_m", .1, 10000.), ("maximum_contact_ground_speed_mps", .01, 10.),
            ("maximum_contact_vertical_speed_mps", .01, 10.), ("maximum_contact_tilt_deg", .1, 10.),
            ("maximum_contact_body_rate_rad_s", .0001, .1), ("minimum_contact_propellant_kg", 0., 100000.),
            ("support_height_m", 0., 200.), ("pin_clearance_center_m", 0., 10.)):
            _require(_number(getattr(self, name), low, high), "invalid_return_site_bounds")
        _require(self.terminal_goal == ("surrogate_pin_support" if self.site_id == "capture" else "model_surface_contact")
            and (len(self.support_points_body_m) == 2 and self.support_height_m > 0 if self.site_id == "capture" else
                self.support_points_body_m == () and self.support_height_m == self.pin_clearance_center_m == self.elevation_m == 0.
                and self.maximum_contact_ground_speed_mps <= 3. and self.maximum_contact_vertical_speed_mps <= 2.
                and self.maximum_contact_tilt_deg <= 5. and self.maximum_contact_body_rate_rad_s <= .02),
            "invalid_return_site_terminal_goal")

    @classmethod
    def from_dict(cls, value):
        value = _copy(value)
        _require(type(value) is dict and set(value) == _SITE_FIELDS and value["schema"] == SITE_SCHEMA
                 and value["site_id"] in ("capture", "divert"), "invalid_return_site_schema")
        for name, low, high in (("latitude_deg", -80., 80.), ("longitude_deg", -180., 180.), ("elevation_m", -100., 5000.),
            ("surface_footprint_radius_m", .1, 10000.), ("maximum_contact_ground_speed_mps", .01, 10.),
            ("maximum_contact_vertical_speed_mps", .01, 10.), ("maximum_contact_tilt_deg", .1, 10.),
            ("maximum_contact_body_rate_rad_s", .0001, .1), ("minimum_contact_propellant_kg", 0., 100000.),
            ("support_height_m", 0., 200.), ("pin_clearance_center_m", 0., 10.)):
            _require(_number(value[name], low, high), "invalid_return_site_bounds")
        points = value["support_points_body_m"]
        if value["site_id"] == "capture":
            _require(value["terminal_goal"] == "surrogate_pin_support" and value["support_height_m"] > 0
                     and type(points) is list and len(points) == 2 and all(_vector(point) for point in points),
                     "invalid_capture_site_goal")
        else:
            _require(value["terminal_goal"] == "model_surface_contact" and value["elevation_m"] == 0.
                     and value["support_height_m"] == value["pin_clearance_center_m"] == 0.
                     and points == [] and value["maximum_contact_ground_speed_mps"] <= 3.
                     and value["maximum_contact_vertical_speed_mps"] <= 2.
                     and value["maximum_contact_tilt_deg"] <= 5. and value["maximum_contact_body_rate_rad_s"] <= .02,
                     "invalid_divert_site_goal")
        return cls(**{key: value[key] for key in _SITE_FIELDS-{"schema", "support_points_body_m"}},
                   support_points_body_m=tuple(tuple(float(x) for x in point) for point in points))

    def to_dict(self):
        return {"schema": SITE_SCHEMA, **{name: getattr(self, name) for name in _SITE_FIELDS-{"schema", "support_points_body_m"}},
                "support_points_body_m": [list(point) for point in self.support_points_body_m]}

    @property
    def sha256(self):
        return sha256(_canonical(self.to_dict())).hexdigest()


def validate_return_site(site, profile, catch_config=None, *, terminal_goal=None):
    """Bind an explicit immutable goal to the unchanged launch declaration.

    Capture is the original surrogate tower only. Diversion is the declared
    synthetic 30 km east surface goal, never another set of support pins.
    This validates geometry; it does not authorize or apply a site change.
    """
    _require(type(site) is ReturnSite, "immutable_return_site_required")
    # Revalidate even a caller-supplied dataclass, without trusting a changed
    # attribute or a mutable stand-in that merely exposes the same properties.
    ReturnSite.from_dict(site.to_dict())
    _require(type(profile) is dict and type(profile.get("launch")) is dict,
             "return_site_original_profile_required")
    launch = profile["launch"]
    _require(_number(launch.get("latitude_deg"), -80., 80.)
        and _number(launch.get("longitude_deg"), -180., 180.), "invalid_return_site_original_coordinates")
    _require(terminal_goal is None or site.terminal_goal == terminal_goal, "return_site_terminal_goal_mismatch")
    if site.site_id == "capture":
        _require(site.latitude_deg == launch["latitude_deg"] and site.longitude_deg == launch["longitude_deg"]
            and site.elevation_m == 0., "capture_site_not_bound_to_original_profile")
        if catch_config is not None:
            _require(type(catch_config) is dict and _number(catch_config.get("support_height_m"), 0., 200.)
                and _number(catch_config.get("initial_pin_clearance_m"), 0., 10.)
                and _number(catch_config.get("arm_half_width_m"), .001, 20.)
                and type(catch_config.get("support_points_body_m")) in (list, tuple)
                and len(catch_config["support_points_body_m"]) == 2
                and all(_vector(point) for point in catch_config["support_points_body_m"]),
                "capture_site_original_support_configuration_required")
            _require(site.support_height_m == catch_config["support_height_m"]
                and site.support_points_body_m == tuple(tuple(point) for point in catch_config["support_points_body_m"])
                and site.pin_clearance_center_m == catch_config["initial_pin_clearance_m"]+.5*catch_config["arm_half_width_m"],
                "capture_site_not_bound_to_original_support")
    else:
        latitude, longitude = east_model_coordinates(launch["latitude_deg"], launch["longitude_deg"])
        _require(site.latitude_deg == latitude and abs(site.longitude_deg-longitude) <= 1e-10,
                 "divert_site_not_declared_30000m_east_model")
    return site


@dataclass(frozen=True)
class ReturnSites:
    profile_id: str
    capture: ReturnSite
    divert: ReturnSite

    def __post_init__(self):
        _require(type(self.profile_id) is str and 1 <= len(self.profile_id) <= 128
            and type(self.capture) is ReturnSite and type(self.divert) is ReturnSite
            and self.capture.site_id == "capture" and self.divert.site_id == "divert", "invalid_immutable_return_sites")
        latitude, longitude = east_model_coordinates(self.capture.latitude_deg, self.capture.longitude_deg)
        _require(self.divert.latitude_deg == latitude and abs(self.divert.longitude_deg-longitude) <= 1e-10,
                 "divert_site_not_declared_30000m_east_model")

    @classmethod
    def from_dict(cls, value, *, profile, catch_config):
        value = _copy(value)
        _require(type(value) is dict and set(value) == _CATALOG_FIELDS and value["schema"] == SCHEMA
            and type(value["profile_id"]) is str and 1 <= len(value["profile_id"]) <= 128
            and value["coordinate_datum"] == "WGS84_ellipsoid" and value["derivation"] == DERIVATION
            and value["nominal_east_distance_m"] == EAST_DISTANCE_M and value["synthetic_model_sites"] is True
            and all(value[key] is False for key in ("surveyed_mission_coordinates", "spacex_clearance_verified",
                "safe_landing_area_verified", "wave_or_water_contact_model"))
            and type(value["sites"]) is list and len(value["sites"]) == 2, "invalid_return_sites_catalog")
        entries = [ReturnSite.from_dict(site) for site in value["sites"]]
        _require([site.site_id for site in entries] == ["capture", "divert"], "return_site_ids_must_be_exactly_capture_divert")
        capture, divert = entries
        launch = profile["launch"]
        _require(capture.latitude_deg == launch["latitude_deg"] and capture.longitude_deg == launch["longitude_deg"]
            and capture.elevation_m == 0. and capture.support_height_m == catch_config["support_height_m"]
            and capture.support_points_body_m == tuple(tuple(point) for point in catch_config["support_points_body_m"])
            and capture.pin_clearance_center_m == catch_config["initial_pin_clearance_m"]+.5*catch_config["arm_half_width_m"],
            "capture_site_not_bound_to_original_profile")
        expected_latitude, expected_longitude = east_model_coordinates(capture.latitude_deg, capture.longitude_deg)
        _require(divert.latitude_deg == expected_latitude and abs(divert.longitude_deg-expected_longitude) <= 1e-10,
                 "divert_site_not_declared_30000m_east_model")
        return cls(value["profile_id"], capture, divert)

    def site(self, site_id):
        _require(type(site_id) is str and site_id in ("capture", "divert"), "unknown_return_site")
        return self.capture if site_id == "capture" else self.divert

    def to_dict(self):
        return {"schema": SCHEMA, "profile_id": self.profile_id, "coordinate_datum": "WGS84_ellipsoid",
            "derivation": DERIVATION, "nominal_east_distance_m": EAST_DISTANCE_M,
            "sites": [self.capture.to_dict(), self.divert.to_dict()], "synthetic_model_sites": True,
            "surveyed_mission_coordinates": False, "spacex_clearance_verified": False,
            "safe_landing_area_verified": False, "wave_or_water_contact_model": False}

    @property
    def sha256(self):
        return sha256(_canonical(self.to_dict())).hexdigest()


def return_site_frame(site, time_s):
    """Earth-fixed target origin and east/north/up axes expressed in ECI."""
    _require(type(site) is ReturnSite and _number(time_s, 0., 1e12), "invalid_return_site_frame")
    latitude = math.radians(site.latitude_deg)
    longitude = math.radians(site.longitude_deg)+EARTH_ROTATION_RAD_S*time_s
    sl, cl, so, co = math.sin(latitude), math.cos(latitude), math.sin(longitude), math.cos(longitude)
    prime = WGS84_A_M/math.sqrt(1-WGS84_E2*sl*sl)
    origin = ((prime+site.elevation_m)*cl*co, (prime+site.elevation_m)*cl*so,
              (prime*(1-WGS84_E2)+site.elevation_m)*sl)
    east, north, up = (-so, co, 0.), (-sl*co, -sl*so, cl), (cl*co, cl*so, sl)
    return {"site_id": site.site_id, "time_s": time_s, "origin_eci_m": list(origin),
        "origin_velocity_eci_mps": [-EARTH_ROTATION_RAD_S*origin[1], EARTH_ROTATION_RAD_S*origin[0], 0.],
        "east_eci": list(east), "north_eci": list(north), "up_eci": list(up),
        "coordinate_datum": "WGS84_ellipsoid", "synthetic_model_site": True}


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _rotate(q, v):
    first = _cross(q[1:], v)
    second = _cross(q[1:], first)
    return tuple(v[i]+2*q[0]*first[i]+2*second[i] for i in range(3))


def state_errors(site, state, *, com_body_m=None, com_rate_body_mps=None):
    """Measured state geometry only; contact thresholds are not a success gate."""
    state = _copy(state)
    if type(site) is ReturnSite and site.support_points_body_m:
        _require(_vector(com_body_m) and _vector(com_rate_body_mps), "capture_material_points_require_measured_centroid")
    else:
        com_body_m = (0., 0., 0.) if com_body_m is None else com_body_m
        com_rate_body_mps = (0., 0., 0.) if com_rate_body_mps is None else com_rate_body_mps
    _require(type(site) is ReturnSite and type(state) is dict and _number(state.get("time_s"), 0., 1e12)
        and all(_vector(state.get(key), 4 if key == "q_body_to_eci" else 3) for key in
            ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s"))
        and _vector(com_body_m) and _vector(com_rate_body_mps), "invalid_return_site_state")
    norm = math.sqrt(sum(x*x for x in state["q_body_to_eci"]))
    _require(abs(norm*norm-1.) <= 1e-8, "invalid_return_site_state_quaternion")
    quaternion = tuple(x/norm for x in state["q_body_to_eci"])
    frame = return_site_frame(site, state["time_s"])
    axes = [frame[key] for key in ("east_eci", "north_eci", "up_eci")]
    def local(vector):
        return [sum(a*b for a, b in zip(vector, axis)) for axis in axes]
    position = local([state["r_eci_m"][i]-frame["origin_eci_m"][i] for i in range(3)])
    rotation = _cross((0., 0., EARTH_ROTATION_RAD_S), state["r_eci_m"])
    velocity = local([state["v_eci_mps"][i]-rotation[i] for i in range(3)])
    body_z = _rotate(quaternion, (0., 0., 1.))
    tilt = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(body_z, frame["up_eci"]))))))
    pins = []
    for point in site.support_points_body_m:
        lever = tuple(point[i]-com_body_m[i] for i in range(3))
        world_lever = _rotate(quaternion, lever)
        physical_position = [state["r_eci_m"][i]+world_lever[i] for i in range(3)]
        rotational_velocity = _rotate(quaternion, _cross(state["omega_body_rad_s"], lever))
        centroid_velocity = _rotate(quaternion, com_rate_body_mps)
        physical_velocity = [state["v_eci_mps"][i]+rotational_velocity[i]-centroid_velocity[i] for i in range(3)]
        ground_rotation = _cross((0., 0., EARTH_ROTATION_RAD_S), physical_position)
        pin_position = local([physical_position[i]-frame["origin_eci_m"][i] for i in range(3)])
        pins.append({"position_enu_m": pin_position,
            "velocity_enu_mps": local([physical_velocity[i]-ground_rotation[i] for i in range(3)]),
            "height_above_support_m": pin_position[2]-site.support_height_m})
    return {"site_id": site.site_id, "time_s": state["time_s"], "position_error_enu_m": position,
        "position_error_frame": "target_site_tangent_enu_not_geodetic_height",
        "ground_velocity_enu_mps": velocity, "horizontal_distance_m": math.hypot(*position[:2]),
        "tilt_deg": tilt, "body_rate_rad_s": math.hypot(*state["omega_body_rad_s"]), "surrogate_pins": pins,
        "state_assigned": False, "contact_or_support_verified": False, "safe_landing_verified": False}
