"""Current recovery policies used at proposal and dispatch boundaries."""

from __future__ import annotations

from typing import Any


LIVE_SITL_RECOVERY_POLICY_REF = (
    "operator_approved_live_sitl_intervention_policy"
)


def live_sitl_recovery_policy() -> dict[str, Any]:
    """Return the currently active bounded live-SITL recovery policy."""

    return {
        "policy_ref": LIVE_SITL_RECOVERY_POLICY_REF,
        "action_feasibility_required": True,
        "preauthorized_actions": [
            "return_to_launch",
            "land",
            "adjust_altitude",
            "adjust_speed",
            "reroute",
            "avoid_obstacle",
            "calibrate_offboard",
        ],
        "battery_return_threshold_percent": 20,
        "max_route_deviation_xy_m": 100,
        "emergency_landing_route_deviation_xy_m": 250,
        "min_terrain_clearance_m": 30,
        "max_wind_speed_mps": 6,
        "max_adjust_altitude_m": 500,
        "max_adjust_speed_mps": 30,
        "max_reroute_target_abs_m": 5000,
        "max_recovery_duration_s": 75,
        # A source-backed route obstacle can require a substantially longer
        # lateral-and-forward leg than other bounded interventions. This
        # extension applies only after Safety HOLD and remains subject to
        # observed performance, battery, wind, terrain, and human approval.
        "max_obstacle_avoidance_duration_s": 240,
        "max_recovery_horizontal_speed_mps": 10,
        "max_recovery_vertical_speed_mps": 3,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5,
        "offboard_performance_envelope_required": True,
        "offboard_performance_min_samples": 5,
        "offboard_performance_uncertainty_fraction": 0.25,
        "offboard_performance_calibration_enabled": True,
        "offboard_performance_calibration_max_distance_m": 15.0,
        "offboard_performance_calibration_max_altitude_delta_m": 2.0,
        "offboard_performance_calibration_speed_mps": 2.0,
        "wind_uncertainty_floor_mps": 1,
        "minimum_motor_thrust_factor": 0.6,
        "obstacle_minimum_clearance_m": 20.0,
        "obstacle_lateral_clearance_m": 30.0,
        "obstacle_buffer_m": 20.0,
        "obstacle_avoidance_climb_m": 15.0,
        "temperature_derating_model": {
            "model_id": "missionos_bounded_sitl_temperature_derating.v1",
            "model_version": "1.0.0",
            "source_refs": [
                "missionos_auto_thermal_weather_simulator_condition_application"
            ],
            "uncertainty_percent": 10.0,
            "battery_capacity_factor": 1.0,
            "motor_thrust_factor": 1.0,
            "calibration_status": "simulation_only_not_physical_calibration",
        },
        "battery_action_energy_model": {
            "model_id": "missionos_bounded_sitl_action_energy.v1",
            "model_version": "1.0.0",
            "source_refs": [
                "missionos_auto_mission_runtime_snapshot",
                "missionos_auto_environment_condition_profile",
            ],
            "uncertainty_percent": 20.0,
            "percent_per_meter_horizontal": 0.02,
            "percent_per_meter_climb": 0.08,
            "headwind_multiplier_per_mps": 0.01,
            "payload_energy_multiplier_per_kg": 0.05,
            "calibration_status": "simulation_only_not_physical_calibration",
        },
    }
