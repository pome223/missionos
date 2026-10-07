"""Independent model-site geometry and signed actor record consistency.

A local integrity signature binds a PID assertion, not OS liveness, human
identity or spacecraft clearance. Catalogue names and later mode flags never
prove safe surface contact, water modelling, capture or physical improvement.
No actor, provider, site producer or dynamics integrator is imported here.
"""

from __future__ import annotations

from hashlib import sha256
import hmac
import json
import math
import re

from .starship_tower_supervision_verifier import verify_tower_supervision, _json
from .starship_booster_recovery_verifier import (
    _flow_and_centroid,
    _state,
    _rotate,
    _cross,
    _profile_and_config,
    _Invalid as _RecoveryInvalid,
)

_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_ROTATION = 7.292115e-5
_CATALOG_FIELDS = {
    "schema",
    "profile_id",
    "coordinate_datum",
    "derivation",
    "nominal_east_distance_m",
    "sites",
    "synthetic_model_sites",
    "surveyed_mission_coordinates",
    "spacex_clearance_verified",
    "safe_landing_area_verified",
    "wave_or_water_contact_model",
}
_SITE_FIELDS = {
    "schema",
    "site_id",
    "latitude_deg",
    "longitude_deg",
    "elevation_m",
    "terminal_goal",
    "surface_footprint_radius_m",
    "maximum_contact_ground_speed_mps",
    "maximum_contact_vertical_speed_mps",
    "maximum_contact_tilt_deg",
    "maximum_contact_body_rate_rad_s",
    "minimum_contact_propellant_kg",
    "support_height_m",
    "pin_clearance_center_m",
    "support_points_body_m",
}


class _Invalid(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Invalid(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _number(value, lower=-1e12, upper=1e12):
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


def _hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _vector(value, length=3):
    return type(value) is list and len(value) == length and all(_number(x) for x in value)


def _near(a, b, tolerance=1e-7):
    return _number(a) and _number(b) and abs(a - b) <= tolerance


def _compare(a, b, tolerance=1e-7):
    _require(
        _vector(a, len(b)) and all(_near(x, y, tolerance) for x, y in zip(a, b)),
        "geometry_vector_mismatch",
    )


def _catalog(catalog, profile, catch, expected_sha=None):
    _json(catalog)
    _require(
        type(profile) is dict
        and type(catch) is dict
        and all(
            type(profile.get(group)) is dict
            for group in ("launch", "booster", "actuators", "guidance")
        ),
        "invalid_closed_profile_structure",
    )
    _profile_and_config(profile, catch)
    _require(
        type(catalog) is dict
        and set(catalog) == _CATALOG_FIELDS
        and catalog["schema"] == "missionos.starship_return_sites.v1"
        and type(catalog["profile_id"]) is str
        and 1 <= len(catalog["profile_id"]) <= 128
        and catalog["coordinate_datum"] == "WGS84_ellipsoid"
        and catalog["derivation"] == "same_geodetic_latitude_wgs84_parallel_arc_30000m_east"
        and catalog["nominal_east_distance_m"] == 30000.0
        and catalog["synthetic_model_sites"] is True
        and all(
            catalog[k] is False
            for k in (
                "surveyed_mission_coordinates",
                "spacex_clearance_verified",
                "safe_landing_area_verified",
                "wave_or_water_contact_model",
            )
        )
        and type(catalog["sites"]) is list
        and len(catalog["sites"]) == 2,
        "invalid_model_catalogue",
    )
    sites = catalog["sites"]
    _require(
        all(type(site) is dict for site in sites)
        and [s.get("site_id") for s in sites] == ["capture", "divert"],
        "invalid_two_site_ids",
    )
    for site in sites:
        _require(
            type(site) is dict
            and set(site) == _SITE_FIELDS
            and site["schema"] == "missionos.starship_return_site.v1",
            "invalid_closed_site",
        )
        for field, lower, upper in (
            ("latitude_deg", -80.0, 80.0),
            ("longitude_deg", -180.0, 180.0),
            ("elevation_m", -100.0, 5000.0),
            ("surface_footprint_radius_m", 0.1, 10000.0),
            ("maximum_contact_ground_speed_mps", 0.01, 10.0),
            ("maximum_contact_vertical_speed_mps", 0.01, 10.0),
            ("maximum_contact_tilt_deg", 0.1, 10.0),
            ("maximum_contact_body_rate_rad_s", 0.0001, 0.1),
            ("minimum_contact_propellant_kg", 0.0, 100000.0),
            ("support_height_m", 0.0, 200.0),
            ("pin_clearance_center_m", 0.0, 10.0),
        ):
            _require(_number(site[field], lower, upper), "invalid_site_bound")
        _require(
            type(site["support_points_body_m"]) is list
            and all(_vector(point) for point in site["support_points_body_m"]),
            "invalid_surrogate_geometry",
        )
    capture, divert = sites
    _require(
        capture["terminal_goal"] == "surrogate_pin_support"
        and capture["latitude_deg"] == profile["launch"]["latitude_deg"]
        and capture["longitude_deg"] == profile["launch"]["longitude_deg"]
        and capture["elevation_m"] == 0.0
        and capture["support_height_m"] == catch["support_height_m"]
        and capture["support_height_m"] > 0.0
        and len(capture["support_points_body_m"]) == 2
        and capture["support_points_body_m"] == catch["support_points_body_m"]
        and capture["pin_clearance_center_m"]
        == catch["initial_pin_clearance_m"] + 0.5 * catch["arm_half_width_m"],
        "capture_profile_geometry_mismatch",
    )
    latitude = math.radians(capture["latitude_deg"])
    prime = _A / math.sqrt(1 - _E2 * math.sin(latitude) ** 2)
    longitude = (
        capture["longitude_deg"] + math.degrees(30000.0 / (prime * math.cos(latitude))) + 180.0
    ) % 360.0 - 180.0
    _require(
        divert["latitude_deg"] == capture["latitude_deg"]
        and _near(divert["longitude_deg"], longitude, 1e-10)
        and divert["terminal_goal"] == "model_surface_contact"
        and divert["support_height_m"]
        == divert["pin_clearance_center_m"]
        == divert["elevation_m"]
        == 0.0
        and divert["support_points_body_m"] == []
        and divert["maximum_contact_ground_speed_mps"] <= 3.0
        and divert["maximum_contact_vertical_speed_mps"] <= 2.0
        and divert["maximum_contact_tilt_deg"] <= 5.0
        and divert["maximum_contact_body_rate_rad_s"] <= 0.02,
        "divert_parallel_arc_or_closed_goal_mismatch",
    )
    checksum = _digest(catalog)
    _require(
        expected_sha is None or _hash(expected_sha) and checksum == expected_sha,
        "catalogue_hash_mismatch",
    )
    return checksum


def verify_return_sites(
    catalog,
    profile,
    catch_config,
    *,
    expected_catalog_sha256=None,
    expected_profile_sha256=None,
    expected_catch_profile_sha256=None,
):
    result = {
        "schema": "missionos.starship_return_sites_verification.v1",
        "passed": False,
        "issues": [],
        "catalogue_sha256": None,
        "capture_geometry_bound": False,
        "declared_parallel_arc_m": 30000.0,
        "synthetic_model_sites": True,
        "surveyed_mission_coordinates": False,
        "spacex_clearance_verified": False,
        "safe_landing_verified": False,
        "wave_or_water_contact_verified": False,
        "physical_execution": False,
        "model_value_established": False,
    }
    try:
        _json(profile)
        _json(catch_config)
        _json(catalog)
        before = _digest(profile), _digest(catch_config), _digest(catalog)
        result["catalogue_sha256"] = _catalog(
            catalog, profile, catch_config, expected_catalog_sha256
        )
        _require(
            expected_profile_sha256 is None or expected_profile_sha256 == before[0],
            "original_profile_hash_mismatch",
        )
        _require(
            expected_catch_profile_sha256 is None or expected_catch_profile_sha256 == before[1],
            "original_catch_profile_hash_mismatch",
        )
        _require(
            before == (_digest(profile), _digest(catch_config), _digest(catalog)), "input_mutation"
        )
        result.update(passed=True, capture_geometry_bound=True)
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append("Invalid bounded model-site declaration or binding")
    return result


def return_site_frame(site, time_s):
    _require(_number(time_s, 0.0, 1e12), "invalid_frame_clock")
    _require(
        type(site) is dict
        and site.get("site_id") in ("capture", "divert")
        and _number(site.get("latitude_deg"), -80.0, 80.0)
        and _number(site.get("longitude_deg"), -180.0, 180.0)
        and _number(site.get("elevation_m"), -100.0, 5000.0),
        "invalid_frame_site",
    )
    latitude = math.radians(site["latitude_deg"])
    longitude = math.radians(site["longitude_deg"]) + _ROTATION * time_s
    sl, cl, so, co = (
        math.sin(latitude),
        math.cos(latitude),
        math.sin(longitude),
        math.cos(longitude),
    )
    radius = _A / math.sqrt(1 - _E2 * sl * sl)
    height = site["elevation_m"]
    origin = [
        (radius + height) * cl * co,
        (radius + height) * cl * so,
        (radius * (1 - _E2) + height) * sl,
    ]
    return {
        "site_id": site["site_id"],
        "time_s": time_s,
        "origin_eci_m": origin,
        "origin_velocity_eci_mps": [-_ROTATION * origin[1], _ROTATION * origin[0], 0.0],
        "east_eci": [-so, co, 0.0],
        "north_eci": [-sl * co, -sl * so, cl],
        "up_eci": [cl * co, cl * so, sl],
        "coordinate_datum": "WGS84_ellipsoid",
        "synthetic_model_site": True,
    }


def state_errors(site, state, profile):
    _state(state, profile)
    frame = return_site_frame(site, state["time_s"])
    axes = [frame[k] for k in ("east_eci", "north_eci", "up_eci")]

    def local(vector):
        return [sum(a * b for a, b in zip(vector, axis)) for axis in axes]

    position = local([a - b for a, b in zip(state["r_eci_m"], frame["origin_eci_m"])])
    rotation = _cross([0.0, 0.0, _ROTATION], state["r_eci_m"])
    velocity = local([a - b for a, b in zip(state["v_eci_mps"], rotation)])
    q = state["q_body_to_eci"]
    norm = math.hypot(*q)
    q = [x / norm for x in q]
    z = _rotate(q, [0.0, 0.0, 1.0])
    tilt = math.degrees(
        math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(z, frame["up_eci"])))))
    )
    _, centroid, centroid_rate, _ = _flow_and_centroid(state, profile)
    pins = []
    for support in site["support_points_body_m"]:
        lever = [a - b for a, b in zip(support, centroid)]
        world = _rotate(q, lever)
        physical_position = [a + b for a, b in zip(state["r_eci_m"], world)]
        angular = _rotate(q, _cross(state["omega_body_rad_s"], lever))
        moving = _rotate(q, centroid_rate)
        physical_velocity = [a + b - c for a, b, c in zip(state["v_eci_mps"], angular, moving)]
        ground = _cross([0.0, 0.0, _ROTATION], physical_position)
        pin_position = local([a - b for a, b in zip(physical_position, frame["origin_eci_m"])])
        pins.append(
            {
                "position_enu_m": pin_position,
                "velocity_enu_mps": local([a - b for a, b in zip(physical_velocity, ground)]),
                "height_above_support_m": pin_position[2] - site["support_height_m"],
            }
        )
    return {
        "site_id": site["site_id"],
        "time_s": state["time_s"],
        "position_error_enu_m": position,
        "position_error_frame": "target_site_tangent_enu_not_geodetic_height",
        "ground_velocity_enu_mps": velocity,
        "horizontal_distance_m": math.hypot(*position[:2]),
        "tilt_deg": tilt,
        "body_rate_rad_s": math.hypot(*state["omega_body_rad_s"]),
        "surrogate_pins": pins,
        "state_assigned": False,
        "contact_or_support_verified": False,
        "safe_landing_verified": False,
    }


def verify_site_errors(record, site, state, profile):
    result = {
        "passed": False,
        "issues": [],
        "geometry_only": True,
        "contact_or_support_verified": False,
        "safe_landing_verified": False,
    }
    try:
        _json(record)
        _json(state)
        _json(site)
        _json(profile)
        expected = state_errors(site, state, profile)
        _require(type(record) is dict and record.keys() == expected.keys(), "site_error_schema")
        for key in ("position_error_enu_m", "ground_velocity_enu_mps"):
            _compare(record[key], expected[key])
        for key in ("horizontal_distance_m", "tilt_deg", "body_rate_rad_s"):
            _require(_near(record[key], expected[key], 1e-5), "site_error_number")
        for key in (
            "site_id",
            "time_s",
            "position_error_frame",
            "state_assigned",
            "contact_or_support_verified",
            "safe_landing_verified",
        ):
            _require(
                type(record[key]) is type(expected[key]) and record[key] == expected[key],
                "site_error_claim_or_identity",
            )
        _require(
            type(record["surrogate_pins"]) is list
            and len(record["surrogate_pins"]) == len(expected["surrogate_pins"]),
            "surrogate_pin_count",
        )
        for actual, pin in zip(record["surrogate_pins"], expected["surrogate_pins"]):
            _require(type(actual) is dict and set(actual) == set(pin), "surrogate_pin_schema")
            _compare(actual["position_enu_m"], pin["position_enu_m"])
            _compare(actual["velocity_enu_mps"], pin["velocity_enu_mps"])
            _require(
                _near(actual["height_above_support_m"], pin["height_above_support_m"]),
                "surrogate_pin_height",
            )
        result["passed"] = True
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append("Invalid independently reconstructed site geometry")
    return result


def _signed(record, key, fields, schema):
    _require(type(key) is bytes and 32 <= len(key) <= 128, "invalid_local_integrity_key")
    _require(
        type(record) is dict
        and set(record) == fields
        and record["schema"] == schema
        and _hash(record["sha256"])
        and _hash(record["signature"]),
        "invalid_signed_actor_record",
    )
    unsigned = {k: v for k, v in record.items() if k not in ("signature", "sha256")}
    _require(
        record["sha256"] == _digest(unsigned)
        and hmac.compare_digest(
            record["signature"],
            hmac.new(
                key, _canonical({k: v for k, v in record.items() if k != "signature"}), "sha256"
            ).hexdigest(),
        ),
        "actor_signature_mismatch",
    )


def verify_tower_actor(
    record,
    catalog,
    profile,
    catch_config,
    *,
    expected_scope,
    local_integrity_key,
    ended_record=None,
    latest_frame=None,
    expected_observations=None,
    expected_integration_record_sha256=None,
):
    result = {
        "schema": "missionos.starship_tower_actor_verification.v1",
        "passed": False,
        "issues": [],
        "identity_signature_valid": False,
        "ended_signature_valid": False,
        "application_signature_valid": False,
        "directive_emitted": False,
        "caller_integration_digest_matched": False,
        "actual_guidance_mode_observed": False,
        "actual_guidance_mode_change_observed": False,
        "os_process_liveness_verified": False,
        "human_identity_verified": False,
        "approval_independently_verified": False,
        "integration_independently_verified": False,
        "physical_effect_verified": False,
        "flight_outcome_improvement": False,
        "physical_surface_outcome_verified": False,
        "safe_landing_verified": False,
        "model_value_established": False,
        "physical_execution": False,
    }
    try:
        for value in (
            record,
            catalog,
            profile,
            catch_config,
            expected_scope,
            ended_record,
            latest_frame,
            expected_observations,
        ):
            _json(value)
        fields = {
            "schema",
            "identity",
            "return_sites_sha256",
            "site_observations",
            "supervision",
            "application_evidence",
            "rejected_inputs",
            "closed",
            "ended",
            "model_calls",
            "provider_keys_read",
            "state_assigned",
            "physical_effect_verified",
            "flight_outcome_improvement",
            "model_value_established",
        }
        _require(
            type(record) is dict
            and set(record) == fields
            and record["schema"] == "missionos.starship_tower_actor.v1"
            and type(record["closed"]) is bool
            and type(record["model_calls"]) is int
            and record["model_calls"] == 0
            and all(
                record[k] is False
                for k in (
                    "provider_keys_read",
                    "state_assigned",
                    "physical_effect_verified",
                    "flight_outcome_improvement",
                    "model_value_established",
                )
            ),
            "actor_scope_or_claim_boundary",
        )
        catalogue_hash = _catalog(catalog, profile, catch_config, record["return_sites_sha256"])
        identity = record["identity"]
        identity_fields = {
            "schema",
            "context",
            "approved_scope_sha256",
            "source_sha256",
            "return_sites_sha256",
            "actor_pid",
            "actor_nonce",
            "started_wall_time_s",
            "provider_keys_read",
            "model_calls",
            "physical_execution",
            "sha256",
            "signature",
        }
        _signed(
            identity,
            local_integrity_key,
            identity_fields,
            "missionos.starship_tower_actor_identity.v1",
        )
        _require(
            identity["context"] == expected_scope["context"]
            and identity["approved_scope_sha256"] == _digest(expected_scope)
            and identity["source_sha256"] == expected_scope["source_sha256"]
            and identity["return_sites_sha256"] == catalogue_hash
            and type(identity["actor_pid"]) is int
            and identity["actor_pid"] > 0
            and type(identity["actor_nonce"]) is str
            and re.fullmatch("[0-9a-f]{32}", identity["actor_nonce"])
            and _number(identity["started_wall_time_s"], 0.0, 1e12)
            and identity["provider_keys_read"] is False
            and identity["physical_execution"] is False
            and type(identity["model_calls"]) is int
            and identity["model_calls"] == 0,
            "actor_identity_context",
        )
        result["identity_signature_valid"] = True
        supervision = verify_tower_supervision(
            record["supervision"],
            expected_scope=expected_scope,
            expected_observations=expected_observations,
            resolution_signing_key=local_integrity_key,
        )
        _require(supervision["passed"], "invalid_actor_supervision")
        observations = record["supervision"]["observations"]
        _require(
            not observations or identity["started_wall_time_s"] <= observations[0]["wall_time_s"],
            "actor_observed_before_started",
        )
        site_observations = record["site_observations"]
        _require(
            type(site_observations) is list and len(site_observations) == len(observations),
            "actor_site_observation_count",
        )
        for actual, obs in zip(site_observations, observations):
            _state(obs["state"], profile)
            _require(
                type(actual) is dict
                and set(actual)
                == {
                    "evidence_sha256",
                    "state_sha256",
                    "simulation_time_s",
                    "wall_time_s",
                    "return_mode",
                    "active_site_id",
                    "return_sites_sha256",
                }
                and all(
                    actual[k] == obs[k]
                    for k in (
                        "evidence_sha256",
                        "state_sha256",
                        "simulation_time_s",
                        "wall_time_s",
                        "return_mode",
                    )
                )
                and _number(actual["simulation_time_s"], 0.0, 1e12)
                and _number(actual["wall_time_s"], identity["started_wall_time_s"], 1e12)
                and actual["return_sites_sha256"] == catalogue_hash
                and actual["active_site_id"] in ("capture", "divert")
                and (
                    obs["return_mode"] == "undecided"
                    or actual["active_site_id"] == obs["return_mode"]
                ),
                "actor_site_or_state_binding",
            )
        _require(
            type(record["rejected_inputs"]) is list
            and len(record["rejected_inputs"]) <= 3
            and all(
                type(row) is dict
                and set(row) == {"file", "reason"}
                and row["file"]
                in ("routing-response-1.json", "routing-response-2.json", "human-response.json")
                and row["reason"] == "invalid_bound_actor_response"
                for row in record["rejected_inputs"]
            )
            and len({row["file"] for row in record["rejected_inputs"]})
            == len(record["rejected_inputs"]),
            "actor_rejection_budget",
        )
        directive = record["supervision"]["directive"]
        app = record["application_evidence"]
        if app is not None:
            app_fields = {
                "schema",
                "context",
                "actor_identity_sha256",
                "directive_sha256",
                "before_state_sha256",
                "after_state_sha256",
                "integration_start_time_s",
                "integration_end_time_s",
                "integration_record_sha256",
                "guidance_target_evidence",
                "supervision_application_report",
                "integration_evidence_supplied",
                "integration_independently_verified",
                "physical_effect_verified",
                "flight_outcome_improvement",
                "model_value_established",
                "physical_execution",
                "sha256",
                "signature",
            }
            _signed(
                app,
                local_integrity_key,
                app_fields,
                "missionos.starship_tower_actor_application.v1",
            )
            _require(
                directive is not None
                and app["context"] == expected_scope["context"]
                and app["actor_identity_sha256"] == identity["sha256"]
                and app["directive_sha256"] == directive["sha256"]
                and app["before_state_sha256"] == directive["state_sha256"]
                and _number(app["integration_start_time_s"], 0.0, 1e12)
                and _number(app["integration_end_time_s"], 0.0, 1e12)
                and app["integration_start_time_s"] == directive["simulation_time_s"]
                and _hash(app["integration_record_sha256"])
                and app["integration_evidence_supplied"] is True
                and all(
                    app[k] is False
                    for k in (
                        "integration_independently_verified",
                        "physical_effect_verified",
                        "flight_outcome_improvement",
                        "model_value_established",
                        "physical_execution",
                    )
                ),
                "actor_application_authority_or_before_state",
            )
            report = record["supervision"]["application_report"]
            _require(
                report is not None
                and app["supervision_application_report"] == report
                and app["after_state_sha256"] == report["state_sha256"]
                and app["integration_end_time_s"] == report["simulation_time_s"]
                and app["integration_end_time_s"] > app["integration_start_time_s"],
                "actor_application_later_state",
            )
            target = app["guidance_target_evidence"]
            site_id = "capture" if directive["action"] == "continue_capture" else "divert"
            _require(
                type(target) is dict
                and set(target)
                == {
                    "context",
                    "return_sites_sha256",
                    "active_site_id",
                    "return_mode",
                    "simulation_time_s",
                    "state_sha256",
                    "target_origin_eci_m",
                }
                and target["context"] == expected_scope["context"]
                and target["return_sites_sha256"] == catalogue_hash
                and target["active_site_id"] == target["return_mode"] == site_id
                and _number(target["simulation_time_s"], 0.0, 1e12)
                and target["simulation_time_s"] == app["integration_end_time_s"]
                and target["state_sha256"] == app["after_state_sha256"],
                "actor_application_target",
            )
            expected = return_site_frame(
                catalog["sites"][int(site_id == "divert")], target["simulation_time_s"]
            )
            _compare(target["target_origin_eci_m"], expected["origin_eci_m"])
            result["application_signature_valid"] = True
            if expected_integration_record_sha256 is not None:
                _require(
                    _hash(expected_integration_record_sha256)
                    and app["integration_record_sha256"] == expected_integration_record_sha256,
                    "external_integration_digest_mismatch",
                )
                result["caller_integration_digest_matched"] = True
            result["actual_guidance_mode_observed"] = (
                supervision["actual_guidance_mode_observed"]
                and result["caller_integration_digest_matched"]
            )
            result["actual_guidance_mode_change_observed"] = (
                supervision["actual_guidance_mode_change_observed"]
                and result["caller_integration_digest_matched"]
            )
        else:
            _require(
                record["supervision"]["application_report"] is None, "unsigned_actor_application"
            )
        ended = record["ended"] if ended_record is None else ended_record
        _require(
            ended_record is None or ended_record == record["ended"], "actor_ended_record_mismatch"
        )
        if record["closed"]:
            ended_fields = {
                "schema",
                "context",
                "actor_identity_sha256",
                "actor_pid",
                "ended_wall_time_s",
                "last_state_sha256",
                "last_simulation_time_s",
                "run_active",
                "physical_outcome_verified",
                "sha256",
                "signature",
            }
            _signed(
                ended, local_integrity_key, ended_fields, "missionos.starship_tower_actor_ended.v1"
            )
            last = observations[-1] if observations else None
            _require(
                ended["context"] == expected_scope["context"]
                and ended["actor_identity_sha256"] == identity["sha256"]
                and ended["actor_pid"] == identity["actor_pid"]
                and ended["run_active"] is False
                and ended["physical_outcome_verified"] is False
                and _number(ended["ended_wall_time_s"], identity["started_wall_time_s"], 1e12)
                and ended["last_state_sha256"] == (last["state_sha256"] if last else None)
                and ended["last_simulation_time_s"] == (last["simulation_time_s"] if last else None)
                and (last is None or ended["ended_wall_time_s"] >= last["wall_time_s"]),
                "actor_ended_lifecycle",
            )
            result["ended_signature_valid"] = True
        else:
            _require(ended is None, "active_actor_cannot_claim_ended")
        if latest_frame is not None:
            _require(
                observations
                and type(latest_frame) is dict
                and set(latest_frame)
                == {
                    "schema",
                    "context",
                    "source_sha256",
                    "observation",
                    "simulation_time_s",
                    "wall_time_s",
                    "run_active",
                }
                and latest_frame["schema"] == "missionos.starship_tower_live_observation_frame.v1"
                and latest_frame["context"] == expected_scope["context"]
                and latest_frame["observation"] == observations[-1]
                and latest_frame["simulation_time_s"] == observations[-1]["simulation_time_s"]
                and _number(latest_frame["wall_time_s"], observations[-1]["wall_time_s"], 1e12)
                and latest_frame["wall_time_s"]
                == (
                    ended["ended_wall_time_s"]
                    if record["closed"]
                    else observations[-1]["wall_time_s"]
                )
                and latest_frame["source_sha256"]
                == record["supervision"]["tick_inputs"][-1]["source_sha256"]
                and latest_frame["run_active"]
                is (
                    False
                    if record["closed"]
                    else record["supervision"]["tick_inputs"][-1]["run_active"]
                ),
                "actor_ipc_lifecycle_or_state_rewrite",
            )
        result.update(passed=True, directive_emitted=supervision["directive_emitted"])
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append("Invalid bound model return-site or signed actor evidence")
    return result
