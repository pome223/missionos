"""Real local weather ingestion for the ``missionos play`` mission-control lab.

Pulls current conditions *and* an hourly forecast from Open-Meteo (JMA model)
so the play simulator can drive the world with real, time-varying weather. The
existing digital-twin weather snapshot
(:func:`src.runtime.digital_twin_mission_environment.build_weather_source_snapshot`)
is current-only and a frozen, golden-hashed boundary, so this module is kept
separate rather than mutating it.

Honesty boundary: the *current* sample and the hourly *forecast* are real,
source-backed data. The vertical wind profile and any sub-hour interpolation are
explicitly **modelled** on top of that real data — see
:func:`wind_at_altitude` and :func:`sample_at_elapsed`, which label themselves as
models. The play layer records what was real vs modelled in the simulator
capability matrix (Stage B).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

# Reuse the validated Open-Meteo JMA endpoint identity from the digital-twin
# module so play stays on exactly the same source contract.
from src.runtime.digital_twin_mission_environment import (
    SOURCE_BACKED_OPEN_METEO_HOST,
    SOURCE_BACKED_OPEN_METEO_JMA_PATH,
    SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX,
)

# fetcher(url) -> (status_text, payload_json_text); mirrors the digital-twin
# weather fetcher injection contract used in the source-backed smokes.
WeatherFetcher = Callable[[str], "tuple[str, str]"]

_DEFAULT_FORECAST_HOURS = 12


class PlayWeatherError(RuntimeError):
    """Raised when an Open-Meteo URL is off-contract."""


def _kmh_to_mps(value: float | None) -> float | None:
    return round(value / 3.6, 3) if value is not None else None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_open_meteo_forecast_url(
    latitude: float,
    longitude: float,
    *,
    forecast_hours: int = _DEFAULT_FORECAST_HOURS,
) -> str:
    """Open-Meteo JMA URL with both current conditions and an hourly forecast."""
    query = urlencode(
        {
            "latitude": round(float(latitude), 7),
            "longitude": round(float(longitude), 7),
            "current": ",".join(
                (
                    "temperature_2m",
                    "precipitation",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                    "surface_pressure",
                )
            ),
            "hourly": ",".join(
                (
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                    "precipitation",
                )
            ),
            "forecast_hours": int(max(1, forecast_hours)),
            "timezone": "UTC",
            "wind_speed_unit": "kmh",
        }
    )
    return f"{SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX}?{query}"


def _validate_open_meteo_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOURCE_BACKED_OPEN_METEO_HOST
        or parsed.path != SOURCE_BACKED_OPEN_METEO_JMA_PATH
    ):
        raise PlayWeatherError("source weather URL must be Open-Meteo JMA HTTPS endpoint")


@dataclass(frozen=True)
class WeatherSample:
    """One weather observation/forecast point. Wind is surface (10 m)."""

    valid_at: str  # ISO8601 (UTC)
    wind_speed_mps: float | None
    wind_gust_mps: float | None
    wind_direction_deg: float | None
    precipitation_mm_per_hour: float | None


@dataclass(frozen=True)
class WindProfileAnchor:
    """A real forecast wind reading at a specific height above ground."""

    height_agl_m: float
    wind_mps: float
    wind_direction_deg: float | None


@dataclass(frozen=True)
class WeatherForecast:
    """Real current + hourly forecast for one location, or an unavailable marker.

    ``wind_profile`` holds real multi-height winds (Open-Meteo 10/80/120/180 m)
    when fetched; when empty, callers fall back to a modelled power-law profile.
    """

    latitude: float
    longitude: float
    source_url: str
    provider_response_status: str
    source_unavailable: bool
    captured_at: str
    current: WeatherSample | None = None
    hourly: tuple[WeatherSample, ...] = field(default_factory=tuple)
    wind_profile: tuple[WindProfileAnchor, ...] = field(default_factory=tuple)
    profile_source_url: str = ""


def _parse_current(payload: dict) -> WeatherSample:
    current = payload.get("current") or {}
    if not current.get("time"):
        raise PlayWeatherError("Open-Meteo response missing current time")
    return WeatherSample(
        valid_at=str(current["time"]),
        wind_speed_mps=_kmh_to_mps(_optional_float(current.get("wind_speed_10m"))),
        wind_gust_mps=_kmh_to_mps(_optional_float(current.get("wind_gusts_10m"))),
        wind_direction_deg=_optional_float(current.get("wind_direction_10m")),
        precipitation_mm_per_hour=_optional_float(current.get("precipitation")),
    )


def _parse_hourly(payload: dict) -> tuple[WeatherSample, ...]:
    hourly = payload.get("hourly") or {}
    times: Sequence = hourly.get("time") or ()
    speeds: Sequence = hourly.get("wind_speed_10m") or ()
    gusts: Sequence = hourly.get("wind_gusts_10m") or ()
    dirs: Sequence = hourly.get("wind_direction_10m") or ()
    precip: Sequence = hourly.get("precipitation") or ()

    def at(seq: Sequence, index: int):
        return seq[index] if index < len(seq) else None

    samples = []
    for index, valid_at in enumerate(times):
        samples.append(
            WeatherSample(
                valid_at=str(valid_at),
                wind_speed_mps=_kmh_to_mps(_optional_float(at(speeds, index))),
                wind_gust_mps=_kmh_to_mps(_optional_float(at(gusts, index))),
                wind_direction_deg=_optional_float(at(dirs, index)),
                precipitation_mm_per_hour=_optional_float(at(precip, index)),
            )
        )
    return tuple(samples)


def fetch_weather_forecast(
    latitude: float,
    longitude: float,
    *,
    fetcher: WeatherFetcher | None = None,
    forecast_hours: int = _DEFAULT_FORECAST_HOURS,
    timeout_seconds: float = 10.0,
    with_profile: bool = False,
    now: datetime | None = None,
) -> WeatherForecast:
    """Fetch real current + hourly forecast. ``fetcher`` lets tests inject data.

    With ``with_profile=True`` it also fetches a real multi-height wind profile
    (Open-Meteo /v1/forecast 10/80/120/180 m) so altitude wind is real data
    rather than a power-law model. On any source failure, returns a forecast
    marked ``source_unavailable`` so the caller can fall back to a bundled
    scenario rather than claiming live weather.
    """
    captured_at = _utc(now).isoformat()
    source_url = build_open_meteo_forecast_url(
        latitude, longitude, forecast_hours=forecast_hours
    )
    try:
        _validate_open_meteo_url(source_url)
        if fetcher is None:
            request = Request(source_url, headers={"User-Agent": "missionos-play"})
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                _validate_open_meteo_url(getattr(response, "url", source_url))
                status = f"http_{getattr(response, 'status', 200)}"
                payload = json.loads(response.read().decode("utf-8"))
        else:
            fetched = fetcher(source_url)
            status = str(fetched[0]) if fetched and fetched[0] else "injected_fetcher"
            payload = json.loads(str(fetched[1]))
        current = _parse_current(payload)
        hourly = _parse_hourly(payload)
    except Exception as exc:  # pragma: no cover - network/parse failure shape varies
        return WeatherForecast(
            latitude=float(latitude),
            longitude=float(longitude),
            source_url=source_url,
            provider_response_status=f"unavailable:{type(exc).__name__}",
            source_unavailable=True,
            captured_at=captured_at,
        )
    wind_profile: tuple[WindProfileAnchor, ...] = ()
    profile_url = ""
    if with_profile:
        wind_profile, profile_url = fetch_wind_profile(
            latitude, longitude, fetcher=fetcher, timeout_seconds=timeout_seconds
        )
    return WeatherForecast(
        latitude=float(latitude),
        longitude=float(longitude),
        source_url=source_url,
        provider_response_status=status,
        source_unavailable=False,
        captured_at=captured_at,
        current=current,
        hourly=hourly,
        wind_profile=wind_profile,
        profile_source_url=profile_url,
    )


_FORECAST_URL_PREFIX = "https://api.open-meteo.com/v1/forecast"
_FORECAST_PATH = "/v1/forecast"
_PROFILE_HEIGHTS_M = (10, 80, 120, 180)


def build_open_meteo_profile_url(latitude: float, longitude: float) -> str:
    """Open-Meteo /v1/forecast URL for real multi-height winds (m/s, no conversion)."""
    speeds = ",".join(f"wind_speed_{h}m" for h in _PROFILE_HEIGHTS_M)
    dirs = ",".join(f"wind_direction_{h}m" for h in _PROFILE_HEIGHTS_M)
    query = urlencode(
        {
            "latitude": round(float(latitude), 7),
            "longitude": round(float(longitude), 7),
            "hourly": f"{speeds},{dirs}",
            "forecast_hours": 1,
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }
    )
    return f"{_FORECAST_URL_PREFIX}?{query}"


def _validate_forecast_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOURCE_BACKED_OPEN_METEO_HOST
        or parsed.path != _FORECAST_PATH
    ):
        raise PlayWeatherError("profile URL must be the Open-Meteo forecast HTTPS endpoint")


def fetch_wind_profile(
    latitude: float,
    longitude: float,
    *,
    fetcher: WeatherFetcher | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[tuple[WindProfileAnchor, ...], str]:
    """Real multi-height wind profile anchors (10/80/120/180 m). Empty on failure."""
    url = build_open_meteo_profile_url(latitude, longitude)
    try:
        _validate_forecast_url(url)
        if fetcher is None:
            request = Request(url, headers={"User-Agent": "missionos-play"})
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        else:
            payload = json.loads(str(fetcher(url)[1]))
        hourly = payload.get("hourly") or {}
        anchors: list[WindProfileAnchor] = []
        for height in _PROFILE_HEIGHTS_M:
            speed_series = hourly.get(f"wind_speed_{height}m") or []
            dir_series = hourly.get(f"wind_direction_{height}m") or []
            speed = _optional_float(speed_series[0] if speed_series else None)
            if speed is None:
                continue
            anchors.append(
                WindProfileAnchor(
                    height_agl_m=float(height),
                    wind_mps=speed,  # already m/s (wind_speed_unit=ms)
                    wind_direction_deg=_optional_float(
                        dir_series[0] if dir_series else None
                    ),
                )
            )
        return tuple(anchors), url
    except Exception:  # pragma: no cover - network/parse failure shape varies
        return (), url


def profile_wind_at(forecast: WeatherForecast, height_agl_m: float) -> float | None:
    """Real interpolated wind at ``height_agl_m`` from the forecast profile.

    Returns ``None`` when no real profile is available (caller falls back to the
    modelled power-law). Linear interpolation between real anchors; clamps to the
    nearest anchor outside the measured band.
    """
    anchors = sorted(forecast.wind_profile, key=lambda a: a.height_agl_m)
    if not anchors:
        return None
    height = max(0.0, float(height_agl_m))
    if height <= anchors[0].height_agl_m:
        return anchors[0].wind_mps
    if height >= anchors[-1].height_agl_m:
        return anchors[-1].wind_mps
    for low, high in zip(anchors, anchors[1:]):
        if low.height_agl_m <= height <= high.height_agl_m:
            span = high.height_agl_m - low.height_agl_m
            frac = 0.0 if span == 0 else (height - low.height_agl_m) / span
            return round(low.wind_mps + (high.wind_mps - low.wind_mps) * frac, 3)
    return anchors[-1].wind_mps


def sample_at_elapsed(
    forecast: WeatherForecast, elapsed_minutes: float
) -> WeatherSample | None:
    """MODELLED: surface weather at ``elapsed_minutes`` into the mission.

    Real anchors are the hourly forecast points; values between them are linearly
    interpolated. This is a model on top of real data, not an observation.
    """
    series = list(forecast.hourly)
    if not series:
        return forecast.current
    hour_index = elapsed_minutes / 60.0
    lower_index = max(0, min(len(series) - 1, int(math.floor(hour_index))))
    upper_index = min(len(series) - 1, lower_index + 1)
    if lower_index == upper_index:
        return series[lower_index]
    fraction = hour_index - lower_index

    def lerp(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return a if b is None else b
        return round(a + (b - a) * fraction, 3)

    low, high = series[lower_index], series[upper_index]
    return WeatherSample(
        valid_at=low.valid_at,
        wind_speed_mps=lerp(low.wind_speed_mps, high.wind_speed_mps),
        wind_gust_mps=lerp(low.wind_gust_mps, high.wind_gust_mps),
        wind_direction_deg=low.wind_direction_deg,
        precipitation_mm_per_hour=lerp(
            low.precipitation_mm_per_hour, high.precipitation_mm_per_hour
        ),
    )
