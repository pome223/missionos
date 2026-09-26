"""Pure, simulator-only AUTO plan export for a stationary ship round trip.

This builds typed MAVLink mission items; it never uploads them. The existing
AUTO probe cannot execute this tape because it recompiles an outbound mission
and uses conditional RTL afterwards. A delivery-aware runtime adapter must
bind release evidence to departure before this export can be dispatched.
"""

from __future__ import annotations

import math
from typing import Any

from src.runtime.missionos_auto_mission_runner import (
    DEFAULT_AUTO_CRUISE_SPEED_MPS,
    DEFAULT_AUTO_MAX_ROUTE_WAYPOINTS,
    DEFAULT_AUTO_WAYPOINT_SPACING_M,
    DEFAULT_DROPOFF_LOITER_SECONDS,
    DEFAULT_LANDING_ALLOWANCE_SECONDS,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    DEFAULT_TAKEOFF_ALLOWANCE_SECONDS,
    DEFAULT_TIMEOUT_SAFETY_FACTOR,
    compile_operator_coordinate_route_auto_mission,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_WAYPOINT,
    PX4GazeboSITLMissionItem,
    SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M,
    SITL_MISSION_UPLOAD_ABSOLUTE_MAX_ALTITUDE_M,
)


def _number(name: str, value: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be finite and between {minimum} and {maximum}")
    return value


def build_stationary_ship_px4_plan(
    *,
    offshore_distance_m: float = 1000.0,
    urban_distance_m: float = 200.0,
    cruise_altitude_m: float = 30.0,
    ship_latitude: float = 35.3195,
    ship_longitude: float = 138.7435,
) -> dict[str, Any]:
    """Export a northbound synthetic route and a retraced return to its home.

    Default coordinates match the existing AUTO probe's home convention; they
    do not assert that the simulated home is geographically on the sea. Sea,
    shoreline, city and deck remain scenario labels until a world is bound.
    """
    offshore = _number("offshore_distance_m", offshore_distance_m, 1.0, 20_000.0)
    urban = _number("urban_distance_m", urban_distance_m, 1.0, 20_000.0)
    altitude = _number(
        "cruise_altitude_m",
        cruise_altitude_m,
        10.0,
        SITL_MISSION_UPLOAD_ABSOLUTE_MAX_ALTITUDE_M,
    )
    latitude = _number("ship_latitude", ship_latitude, -85.0, 85.0)
    longitude = _number("ship_longitude", ship_longitude, -180.0, 180.0)
    outbound_m = offshore + urban
    if outbound_m > SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M:
        raise ValueError("outbound route exceeds the existing SITL geofence envelope")
    duration_s = (
        2.0 * outbound_m / DEFAULT_AUTO_CRUISE_SPEED_MPS
        + DEFAULT_TAKEOFF_ALLOWANCE_SECONDS
        + DEFAULT_DROPOFF_LOITER_SECONDS
        + DEFAULT_LANDING_ALLOWANCE_SECONDS
    )
    timeout_s = duration_s * DEFAULT_TIMEOUT_SAFETY_FACTOR
    if timeout_s > DEFAULT_MAX_TIMEOUT_SECONDS:
        raise ValueError("round-trip timeout exceeds the existing AUTO runtime envelope")

    def north_lat(distance_m: float) -> float:
        return round(latitude + math.degrees(distance_m / 6_371_000.0), 7)

    route = {
        "schema_version": "mission_designer_coordinate_pair_route.v1",
        "route_id": "stationary_ship_delivery_outbound",
        "takeoff_latitude": round(latitude, 7),
        "takeoff_longitude": round(longitude, 7),
        "dropoff_latitude": north_lat(outbound_m),
        "dropoff_longitude": round(longitude, 7),
        # The legacy compiler consumes this field as its flat-world altitude.
        # It is an adapter parameter here, not evidence of a surveyed rooftop.
        "dropoff_roof_height_agl_m": altitude,
        "derived_route_distance_m": outbound_m,
    }
    compilation = compile_operator_coordinate_route_auto_mission(route)
    outbound = [item for item in compilation.mission_items if item.command == MAV_CMD_NAV_WAYPOINT]
    shore_lat = north_lat(offshore)
    if not any(item.latitude_deg == shore_lat for item in outbound):
        outbound.append(
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_WAYPOINT,
                latitude_deg=shore_lat,
                longitude_deg=round(longitude, 7),
                altitude_m=altitude,
            )
        )
        outbound.sort(key=lambda item: item.latitude_deg)
    if 2 * len(outbound) > DEFAULT_AUTO_MAX_ROUTE_WAYPOINTS:
        raise ValueError("round-trip waypoint count exceeds the existing AUTO route envelope")
    home = PX4GazeboSITLMissionItem(
        seq=0,
        command=MAV_CMD_NAV_WAYPOINT,
        latitude_deg=round(latitude, 7),
        longitude_deg=round(longitude, 7),
        altitude_m=altitude,
    )
    dwell = compilation.mission_items[int(compilation.dropoff_dwell_mission_seq)]
    tape = [
        compilation.mission_items[0],
        *outbound,
        dwell,
        *reversed(outbound[:-1]),
        home,
        home.model_copy(update={"command": MAV_CMD_NAV_LAND, "altitude_m": 0.0}),
    ]
    items = [
        item.model_copy(update={"seq": seq}).model_dump(mode="json")
        for seq, item in enumerate(tape)
    ]
    shore_outbound_seq = next(
        seq
        for seq, item in enumerate(tape)
        if item.command == MAV_CMD_NAV_WAYPOINT and item.latitude_deg == shore_lat
    )
    return {
        "schema_version": "missionos_stationary_ship_px4_plan.v1",
        "status": "blocked_pending_runtime_adapter",
        "simulation_only": True,
        "vehicle_count": 1,
        "ship_motion": "stationary",
        "coordinate_frame": "synthetic_flat_world_global_relative_altitude",
        "outbound_distance_m": outbound_m,
        "round_trip_distance_m": 2.0 * outbound_m,
        "offshore_distance_m": offshore,
        "urban_distance_m": urban,
        "cruise_altitude_m": altitude,
        "waypoint_spacing_m": DEFAULT_AUTO_WAYPOINT_SPACING_M,
        "expected_duration_seconds": duration_s,
        "timeout_seconds": timeout_s,
        "operator_coordinate_route": route,
        "outbound_compilation": compilation.model_dump(mode="json"),
        "mission_items": items,
        "shoreline_outbound_seq": shore_outbound_seq,
        "dropoff_dwell_mission_seq": len(outbound) + 1,
        "return_start_mission_seq": len(outbound) + 2,
        "land_mission_seq": len(items) - 1,
        "blocked_reasons": [
            "ship_delivery_runtime_adapter_not_implemented",
            "payload_release_observation_not_bound_to_return_departure",
            "ship_deck_and_urban_world_not_bound",
            "return_and_deck_landing_observations_not_available",
        ],
        "limitations": [
            "Plan export is not PX4 upload, execution, or a simulator result.",
            "The timed dropoff loiter does not release cargo or wait for verified delivery.",
            "Existing AUTO probe recompiles outbound then conditionally commands RTL; it does not consume this round-trip tape.",
            "Sea and city labels do not establish GPS, wind, building, VLA, or WAM behavior.",
            "Energy, payload touchdown, stationary-deck contact and stable recovery require separate observations.",
        ],
        "operator_approval_required_before_dispatch": True,
        "live_execution_supported": False,
        "runtime_invoked": False,
        "mavlink_dispatch_performed": False,
        "payload_release_observed": False,
        "delivery_completion_claimed": False,
        "ship_recovery_verified": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
