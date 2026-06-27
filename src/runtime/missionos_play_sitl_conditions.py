"""Map real weather into PX4/Gazebo SITL condition parameters for ``missionos play``.

Takes a real Open-Meteo forecast (:mod:`src.runtime.missionos_play_weather`) and
produces the ``MISSION_DESIGNER_REALISM_*`` environment the live SITL runner
already consumes (``src/runtime/px4_gazebo_mission_designer_sitl_runner.py``,
``MISSION_DESIGNER_REALISM_WIND_MEAN_MPS`` / ``_WIND_GUST_MPS`` /
``_WIND_VARIANCE`` / ``_PAYLOAD_MASS_KG``).

Honesty boundary — what is real vs modelled vs unsupported is recorded in a
capability matrix shaped like the runner's own
``simulator_capability_matrix`` / ``simulator_condition_application``:

- wind mean / gust at the surface  -> **real** (Open-Meteo), applied
- wind at flight altitude          -> **modelled** (power-law profile)
- wind variance                    -> **modelled** (derived from gust spread)
- wind direction                   -> **unsupported** (no SITL env knob today)
- time-varying wind over a mission -> **approximated** (forwarded as a launch
  mean; mid-run re-forwarding is not wired into the runner env yet)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.runtime.missionos_play_weather import (
    WeatherForecast,
    WeatherSample,
    profile_wind_at,
    sample_at_elapsed,
)

# Power-law wind profile exponent. ~0.143 is the classic open-terrain value;
# mountainous/forested terrain shears more, so the play default is a bit higher.
# This is a MODEL, not measured truth.
_WIND_PROFILE_EXPONENT = 0.20
_PROFILE_REFERENCE_HEIGHT_M = 10.0  # Open-Meteo 10 m surface wind


def wind_at_altitude(
    surface_mps: float,
    height_agl_m: float,
    *,
    exponent: float = _WIND_PROFILE_EXPONENT,
) -> float:
    """MODELLED: extrapolate 10 m surface wind to ``height_agl_m`` (power law).

    v(h) = v_10 * (h / 10)^exponent. Real data is the 10 m surface wind; the
    vertical increase is a model.
    """
    if surface_mps <= 0:
        return 0.0
    height = max(_PROFILE_REFERENCE_HEIGHT_M, float(height_agl_m))
    return round(surface_mps * (height / _PROFILE_REFERENCE_HEIGHT_M) ** exponent, 3)


@dataclass(frozen=True)
class WindTimePoint:
    elapsed_minutes: float
    surface_wind_mps: float | None
    altitude_wind_mps: float | None
    gust_mps: float | None


@dataclass(frozen=True)
class PlaySitlConditions:
    """SITL realism env + an honest record of what is real/modelled/unsupported."""

    realism_env: dict[str, str]
    capability_matrix: dict[str, object]
    condition_application: dict[str, object]
    wind_time_series: tuple[WindTimePoint, ...] = field(default_factory=tuple)
    source_unavailable: bool = False


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def build_sitl_conditions(
    forecast: WeatherForecast,
    *,
    flight_agl_m: float,
    payload_kg: float,
    mission_minutes: float = 60.0,
    step_minutes: float = 10.0,
) -> PlaySitlConditions:
    """Build the live-SITL realism env from a real forecast at a flight altitude."""

    if forecast.source_unavailable or forecast.current is None:
        return PlaySitlConditions(
            realism_env={"MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": f"{payload_kg:.3f}"},
            capability_matrix={
                "schema_version": "play_simulator_capability_matrix.v1",
                "records": "play_mapping_only; sim application recorded by runner matrix",
                "wind_mean": "source_unavailable",
                "wind_gust": "source_unavailable",
                "wind_variance": "source_unavailable",
                "wind_direction": "source_unavailable",
                "altitude_profile": "source_unavailable",
                "time_varying": "source_unavailable",
                "approximation_reasons": ["weather_source_unavailable"],
            },
            condition_application={
                "schema_version": "play_simulator_condition_application.v1",
                "application_status": "source_unavailable",
                "delivery_completion_claimed": False,
                "physical_execution_invoked": False,
            },
            source_unavailable=True,
        )

    # Prefer the real multi-height forecast profile; fall back to the modelled
    # power-law only when no real profile was fetched.
    use_real_profile = bool(forecast.wind_profile)
    real_profile_wind = profile_wind_at(forecast, flight_agl_m) if use_real_profile else None

    def _altitude_wind(surface_value: float | None) -> float | None:
        if surface_value is None:
            return None
        if real_profile_wind is not None:
            return real_profile_wind  # real measured/forecast wind at this height
        return wind_at_altitude(surface_value, flight_agl_m)

    def _altitude_gust(surface_value: float | None, surface_mean: float | None) -> float | None:
        if surface_value is None:
            return None
        if real_profile_wind is not None and surface_mean:
            # scale the real surface gust by the real profile's altitude ratio
            return round(surface_value * (real_profile_wind / surface_mean), 3)
        return wind_at_altitude(surface_value, flight_agl_m)

    # Build the wind time series at flight altitude across the mission.
    series: list[WindTimePoint] = []
    elapsed = 0.0
    while elapsed <= mission_minutes:
        sample: WeatherSample | None = sample_at_elapsed(forecast, elapsed)
        surface = sample.wind_speed_mps if sample else None
        gust_surface = sample.wind_gust_mps if sample else None
        series.append(
            WindTimePoint(
                elapsed_minutes=elapsed,
                surface_wind_mps=_round(surface),
                altitude_wind_mps=_altitude_wind(surface),
                gust_mps=_altitude_gust(gust_surface, surface),
            )
        )
        elapsed += step_minutes

    launch = series[0]
    launch_mean = launch.altitude_wind_mps or 0.0
    launch_gust = launch.gust_mps or 0.0
    # Variance modelled from the gust-to-mean spread (a simple, legible proxy).
    wind_variance = round(max(0.0, (launch_gust - launch_mean) / 2.0), 3)
    # Wind direction and precipitation are real surface values; they do not scale
    # with altitude in this model, so the surface reading is forwarded as-is.
    launch_direction = forecast.current.wind_direction_deg
    launch_precip = forecast.current.precipitation_mm_per_hour

    realism_env = {
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS": f"{launch_mean:.3f}",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS": f"{launch_gust:.3f}",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE": f"{wind_variance:.3f}",
        "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": f"{payload_kg:.3f}",
    }
    if launch_direction is not None:
        realism_env["MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"] = f"{launch_direction:.3f}"
    if launch_precip is not None:
        realism_env["MISSION_DESIGNER_REALISM_PRECIPITATION_MM_PER_HOUR"] = f"{launch_precip:.3f}"

    approximation_reasons = ["wind_variance_derived_from_gust_spread"]
    if not use_real_profile:
        approximation_reasons.insert(0, "wind_at_altitude_power_law_modelled")
    if any((p.altitude_wind_mps or 0.0) != launch_mean for p in series[1:]):
        # The forecast genuinely varies over the mission, but the runner env only
        # takes a launch value today -> be explicit that the mid-run change is
        # forwarded as a single launch value, not re-applied during flight.
        approximation_reasons.append("time_varying_wind_forwarded_as_launch_value")

    # This matrix describes what THIS mapping forwards (forwarded_real / modelled
    # / derived). Whether the Gazebo/PX4 runner actually reproduces each forwarded
    # value is recorded separately by the runner's own simulator_capability_matrix.
    capability_matrix = {
        "schema_version": "play_simulator_capability_matrix.v1",
        "records": "play_mapping_only; sim application recorded by runner matrix",
        "wind_mean": "forwarded_real_surface",
        "wind_gust": "forwarded_real_surface",
        "wind_direction": (
            "forwarded_real_surface" if launch_direction is not None else "absent_in_source"
        ),
        "precipitation": (
            "forwarded_real_surface" if launch_precip is not None else "absent_in_source"
        ),
        "wind_variance": "derived",
        "altitude_profile": (
            "real_forecast_profile" if use_real_profile else "modelled_power_law"
        ),
        "time_varying": "forwarded_launch_value_only",
        "approximation_reasons": approximation_reasons,
        "delivery_completion_claimed": False,
    }
    condition_application = {
        "schema_version": "play_simulator_condition_application.v1",
        "application_status": "forwarded_with_approximations",
        "weather_source_url": forecast.source_url,
        "weather_provider_status": forecast.provider_response_status,
        "flight_agl_m": float(flight_agl_m),
        "forwarded_launch_wind_mean_mps": launch_mean,
        "forwarded_launch_wind_gust_mps": launch_gust,
        "forwarded_wind_direction_deg": launch_direction,
        "surface_wind_mean_mps": launch.surface_wind_mps,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    return PlaySitlConditions(
        realism_env=realism_env,
        capability_matrix=capability_matrix,
        condition_application=condition_application,
        wind_time_series=tuple(series),
        source_unavailable=False,
    )
