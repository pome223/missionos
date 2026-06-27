"""Stage 1 digital-twin planning artifacts for Mission Designer.

These artifacts bind prompt-derived real-world constraints to reproducible
planning evidence. Stage 2 starts with fixture-backed target-resolution
candidates, but this module still does not call a live geocoder, fetch DEM or
weather data, execute Gazebo worlds, upload PX4 missions, or grant execution
authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION = "real_world_mission_target.v1"
REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION = "real_world_geocode_candidate.v1"
REAL_WORLD_TARGET_RESOLUTION_SCHEMA_VERSION = "real_world_target_resolution.v1"
TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION = (
    "terrain_dem_tile_request_candidate.v1"
)
TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION = "terrain_dem_tile_snapshot.v1"
TERRAIN_DEM_SOURCE_SNAPSHOT_SCHEMA_VERSION = "terrain_dem_source_snapshot.v1"
TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION = (
    "tile_backed_terrain_environment_snapshot.v1"
)
TERRAIN_HEIGHTMAP_CANDIDATE_SCHEMA_VERSION = "terrain_heightmap_candidate.v1"
TERRAIN_HEIGHTMAP_ARTIFACT_SCHEMA_VERSION = "terrain_heightmap_artifact.v1"
TERRAIN_HEIGHTMAP_FILE_ARTIFACT_SCHEMA_VERSION = (
    "terrain_heightmap_file_artifact.v1"
)
GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION = "gazebo_world_candidate.v1"
GAZEBO_WORLD_ARTIFACT_SCHEMA_VERSION = "gazebo_world_artifact.v1"
COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION = (
    "coordinate_transform_candidate.v1"
)
DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION = (
    "digital_twin_mission_anchor_candidate.v1"
)
DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION = (
    "digital_twin_px4_mission_item_candidate.v1"
)
DIGITAL_TWIN_SITL_BINDING_GATE_SCHEMA_VERSION = "digital_twin_sitl_binding_gate.v1"
TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION = "terrain_environment_snapshot.v1"
WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION = "weather_environment_snapshot.v1"
WEATHER_SOURCE_SNAPSHOT_SCHEMA_VERSION = "weather_source_snapshot.v1"
DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION = "digital_twin_route_feasibility.v1"
WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION = (
    "weather_environment_policy_gate.v1"
)
DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION = "digital_twin_route_plan.v1"
VEHICLE_FLIGHT_ENVELOPE_SCHEMA_VERSION = "vehicle_flight_envelope.v1"
MISSION_ENERGY_BUDGET_SCHEMA_VERSION = "mission_energy_budget.v1"
DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION = "digital_twin_stage1_epic_exit.v1"
STAGE1_TARGET_SOURCE_URL = "prompt://operator"
STAGE2_GEOCODE_SOURCE_URL = "fixture://boiled-claw/digital-twin/stage2-geocode"
TAKEOFF_FROM_TARGET_BEARING_DEG = 270.0
SOURCE_BACKED_TARGET_RESOLUTION_SOURCE_URL = "operator://confirmed-wgs84-target"
SOURCE_BACKED_GSI_DEM_TILE_URL_TEMPLATE = (
    "https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt"
)
SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX = (
    "https://cyberjapandata.gsi.go.jp/xyz/dem/"
)
SOURCE_BACKED_GSI_DEM_HOST = "cyberjapandata.gsi.go.jp"
SOURCE_BACKED_PROVIDER_USER_AGENT = (
    "boiled-claw-digital-twin/1.0 (+https://github.com/pome223/boiled-claw)"
)
SOURCE_BACKED_HEIGHTMAP_SAMPLE_WIDTH = 64
SOURCE_BACKED_HEIGHTMAP_SAMPLE_HEIGHT = 64
SOURCE_BACKED_MAX_DEM_TILE_COUNT = 64
STAGE2_DEM_TILE_INDEX_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-dem-tile-index"
)
STAGE2_DEM_TILE_SNAPSHOT_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-dem-tile-snapshot"
)
STAGE2_TILE_BACKED_TERRAIN_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-tile-backed-terrain"
)
STAGE2_HEIGHTMAP_CANDIDATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-heightmap-candidate"
)
STAGE2_HEIGHTMAP_ARTIFACT_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-heightmap-artifact"
)
STAGE2_HEIGHTMAP_FILE_ARTIFACT_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-heightmap-file-artifact"
)
STAGE2_GAZEBO_WORLD_CANDIDATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-gazebo-world-candidate"
)
STAGE2_GAZEBO_WORLD_ARTIFACT_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-gazebo-world-artifact"
)
STAGE2_COORDINATE_TRANSFORM_CANDIDATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-coordinate-transform-candidate"
)
STAGE2_MISSION_ANCHOR_CANDIDATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-mission-anchor-candidate"
)
STAGE2_PX4_MISSION_ITEM_CANDIDATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-px4-mission-item-candidate"
)
STAGE2_SITL_BINDING_GATE_SOURCE_URL = (
    "fixture://boiled-claw/digital-twin/stage2-sitl-binding-gate"
)
STAGE1_TERRAIN_SOURCE_URL = "fixture://boiled-claw/digital-twin/stage1-dem"
STAGE1_WEATHER_SOURCE_URL = "prompt://operator"
VEHICLE_PROFILE_ROOT = Path("src/runtime/digital_twin_vehicle_profiles")
VEHICLE_PROFILE_ROOT_ABS = Path(__file__).resolve().parent / (
    "digital_twin_vehicle_profiles"
)
DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH = (
    VEHICLE_PROFILE_ROOT_ABS / "missionos_fixture_quadrotor.json"
)
PAYLOAD_ENERGY_MARGIN_WH_PER_KG = 8.0
WIND_ENERGY_MARGIN_WH_PER_MPS = 3.0
TAKEOFF_AGL_MARGIN_M = 15.0
SOURCE_BACKED_ROUTE_MARGIN_M = 100.0
SOURCE_BACKED_TERRAIN_CONTEXT_MIN_EXTENT_M = 1_200.0
SOURCE_BACKED_TERRAIN_CONTEXT_PADDING_RATIO = 0.35
SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX = "https://api.open-meteo.com/v1/jma"
SOURCE_BACKED_OPEN_METEO_HOST = "api.open-meteo.com"
SOURCE_BACKED_OPEN_METEO_JMA_PATH = "/v1/jma"
HEIGHTMAP_FILE_ARTIFACT_ROOT = Path("output/digital_twin/heightmaps")
GAZEBO_WORLD_ARTIFACT_ROOT = Path("output/digital_twin/worlds")
HEIGHTMAP_FILE_PAYLOAD_SCHEMA_VERSION = "terrain_heightmap_file_payload.v1"

_FULLWIDTH_NUMBER_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、",
    "0123456789.,,",
)
_DISTANCE_PATTERNS = (
    re.compile(
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:km|kilometer|kilometers|キロメートル|キロ)",
        re.IGNORECASE,
    ),
)


class DigitalTwinMissionEnvironmentError(RuntimeError):
    """Raised when digital-twin planning evidence is unsafe or inconsistent."""


class _DigitalTwinPlanningBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    planning_only: Literal[True] = True
    source_bound: Literal[True] = True
    reproducible: Literal[True] = True
    gazebo_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _content_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _text_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _normalize_numeric_text(text: str) -> str:
    return text.translate(_FULLWIDTH_NUMBER_TRANSLATION)


def _extract_distance_km(prompt: str) -> float | None:
    normalized = _normalize_numeric_text(prompt)
    for pattern in _DISTANCE_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        try:
            return float(match.group("value").replace(",", ""))
        except ValueError:
            return None
    return None


def _target_label(prompt: str) -> str:
    lowered = prompt.lower()
    if "山小屋" in prompt:
        return "mountain_hut"
    if "山頂" in prompt or "summit" in lowered or "peak" in lowered:
        return "mountain_summit"
    if "山" in prompt or "mountain" in lowered:
        return "mountain_target"
    return "operator_prompt_target"


def _target_resolution_status(prompt: str) -> Literal[
    "prompt_target_unresolved",
    "prompt_target_ambiguous",
]:
    if "どこか" in prompt or "somewhere" in prompt.lower():
        return "prompt_target_ambiguous"
    return "prompt_target_unresolved"


def _bbox_around_wgs84(
    latitude: float,
    longitude: float,
    *,
    delta_degrees: float = 0.02,
) -> tuple[float, float, float, float]:
    return (
        round(max(-90.0, latitude - delta_degrees), 7),
        round(max(-180.0, longitude - delta_degrees), 7),
        round(min(90.0, latitude + delta_degrees), 7),
        round(min(180.0, longitude + delta_degrees), 7),
    )


def _haversine_distance_m(
    *,
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_m = 6_371_000.0
    lat_a = math.radians(float(latitude_a))
    lat_b = math.radians(float(latitude_b))
    delta_lat = math.radians(float(latitude_b) - float(latitude_a))
    delta_lon = math.radians(float(longitude_b) - float(longitude_a))
    haversine = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * radius_m * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1.0 - haversine),
    )


def _wgs84_destination(
    *,
    origin_latitude: float,
    origin_longitude: float,
    bearing_deg: float,
    distance_m: float,
) -> tuple[float, float]:
    radius_m = 6_371_000.0
    angular_distance = float(distance_m) / radius_m
    bearing = math.radians(float(bearing_deg))
    lat_1 = math.radians(float(origin_latitude))
    lon_1 = math.radians(float(origin_longitude))
    lat_2 = math.asin(
        math.sin(lat_1) * math.cos(angular_distance)
        + math.cos(lat_1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon_2 = lon_1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat_1),
        math.cos(angular_distance) - math.sin(lat_1) * math.sin(lat_2),
    )
    lon_deg = (math.degrees(lon_2) + 540.0) % 360.0 - 180.0
    return round(math.degrees(lat_2), 7), round(lon_deg, 7)


def _bbox_covering_wgs84_points(
    points: Sequence[tuple[float, float]],
    *,
    margin_degrees: float = 0.02,
) -> tuple[float, float, float, float]:
    latitudes = [float(point[0]) for point in points]
    longitudes = [float(point[1]) for point in points]
    return (
        round(max(-90.0, min(latitudes) - margin_degrees), 7),
        round(max(-180.0, min(longitudes) - margin_degrees), 7),
        round(min(90.0, max(latitudes) + margin_degrees), 7),
        round(min(180.0, max(longitudes) + margin_degrees), 7),
    )


def _source_backed_terrain_context_bbox(
    bbox: Sequence[float],
    *,
    minimum_extent_m: float = SOURCE_BACKED_TERRAIN_CONTEXT_MIN_EXTENT_M,
    padding_ratio: float = SOURCE_BACKED_TERRAIN_CONTEXT_PADDING_RATIO,
) -> tuple[float, float, float, float]:
    lat_min, lon_min, lat_max, lon_max = (float(item) for item in bbox)
    center_lat = (lat_min + lat_max) / 2.0
    center_lon = (lon_min + lon_max) / 2.0
    lat_span_m = _haversine_distance_m(
        latitude_a=lat_min,
        longitude_a=center_lon,
        latitude_b=lat_max,
        longitude_b=center_lon,
    )
    lon_span_m = _haversine_distance_m(
        latitude_a=center_lat,
        longitude_a=lon_min,
        latitude_b=center_lat,
        longitude_b=lon_max,
    )
    base_extent_m = max(lat_span_m, lon_span_m, float(minimum_extent_m))
    context_extent_m = base_extent_m * (1.0 + max(0.0, float(padding_ratio)) * 2.0)
    half_extent_m = context_extent_m / 2.0
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = max(
        1_000.0,
        meters_per_degree_lat * abs(math.cos(math.radians(center_lat))),
    )
    lat_delta = half_extent_m / meters_per_degree_lat
    lon_delta = half_extent_m / meters_per_degree_lon
    return (
        round(max(-90.0, min(lat_min, center_lat - lat_delta)), 7),
        round(max(-180.0, min(lon_min, center_lon - lon_delta)), 7),
        round(min(90.0, max(lat_max, center_lat + lat_delta)), 7),
        round(min(180.0, max(lon_max, center_lon + lon_delta)), 7),
    )


def _wgs84_inside_bbox(
    *,
    latitude: float,
    longitude: float,
    bbox: Sequence[float],
) -> bool:
    lat_min, lon_min, lat_max, lon_max = (float(item) for item in bbox)
    return (
        lat_min <= float(latitude) <= lat_max
        and lon_min <= float(longitude) <= lon_max
    )


def _web_mercator_tile_for_wgs84(
    latitude: float,
    longitude: float,
    *,
    zoom: int = 14,
) -> tuple[int, int, int]:
    lat_rad = math.radians(max(min(latitude, 85.05112878), -85.05112878))
    n = 2**zoom
    x = int((longitude + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    )
    return zoom, x, y


def _web_mercator_tile_float_for_wgs84(
    latitude: float,
    longitude: float,
    *,
    zoom: int = 14,
) -> tuple[int, float, float]:
    lat_rad = math.radians(max(min(latitude, 85.05112878), -85.05112878))
    n = 2**zoom
    x = (longitude + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return zoom, x, y


def _gsi_dem_tile_range_for_bbox(
    bbox: Sequence[float],
    *,
    zoom: int = 14,
) -> tuple[tuple[int, int, int], ...]:
    lat_min, lon_min, lat_max, lon_max = (float(item) for item in bbox)
    corners = (
        _web_mercator_tile_for_wgs84(lat_min, lon_min, zoom=zoom),
        _web_mercator_tile_for_wgs84(lat_min, lon_max, zoom=zoom),
        _web_mercator_tile_for_wgs84(lat_max, lon_min, zoom=zoom),
        _web_mercator_tile_for_wgs84(lat_max, lon_max, zoom=zoom),
    )
    x_min = min(item[1] for item in corners)
    x_max = max(item[1] for item in corners)
    y_min = min(item[2] for item in corners)
    y_max = max(item[2] for item in corners)
    coords = tuple(
        (zoom, x, y)
        for y in range(y_min, y_max + 1)
        for x in range(x_min, x_max + 1)
    )
    if len(coords) > SOURCE_BACKED_MAX_DEM_TILE_COUNT:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM bbox requires too many GSI DEM tiles"
        )
    return coords


def _gsi_dem_tile_url(zoom: int, x: int, y: int) -> str:
    return SOURCE_BACKED_GSI_DEM_TILE_URL_TEMPLATE.format(z=zoom, x=x, y=y)


def _open_meteo_jma_url(latitude: float, longitude: float) -> str:
    query = urlencode(
        {
            "latitude": round(latitude, 7),
            "longitude": round(longitude, 7),
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
            "timezone": "UTC",
            "wind_speed_unit": "kmh",
        }
    )
    return f"{SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX}?{query}"


def _validate_gsi_dem_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOURCE_BACKED_GSI_DEM_HOST
        or not source_url.startswith(SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX)
    ):
        raise DigitalTwinMissionEnvironmentError(
            "source DEM URL must be GSI HTTPS endpoint"
        )


def _validate_open_meteo_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOURCE_BACKED_OPEN_METEO_HOST
        or parsed.path != SOURCE_BACKED_OPEN_METEO_JMA_PATH
    ):
        raise DigitalTwinMissionEnvironmentError(
            "source weather URL must be Open-Meteo JMA HTTPS endpoint"
        )


def _parse_gsi_dem_txt(payload: str) -> tuple[tuple[float, ...], float]:
    values: list[float] = []
    total_cells = 0
    for row in payload.splitlines():
        for cell in row.split(","):
            total_cells += 1
            text = cell.strip()
            if not text or text.lower() in {"e", "nan", "null", "none"}:
                continue
            try:
                values.append(float(text))
            except ValueError:
                continue
    if total_cells == 0 or not values:
        raise DigitalTwinMissionEnvironmentError("GSI DEM response did not include elevations")
    no_data_ratio = round(1.0 - (len(values) / total_cells), 6)
    return tuple(values), no_data_ratio


def _parse_gsi_dem_grid_txt(
    payload: str,
) -> tuple[tuple[float | None, ...], int, int, tuple[float, ...], float]:
    rows: list[list[float | None]] = []
    values: list[float] = []
    width = 0
    for line in payload.splitlines():
        cells = line.split(",")
        if not cells:
            continue
        parsed_row: list[float | None] = []
        for cell in cells:
            text = cell.strip()
            if not text or text.lower() in {"e", "nan", "null", "none"}:
                parsed_row.append(None)
                continue
            try:
                elevation = float(text)
            except ValueError:
                parsed_row.append(None)
                continue
            parsed_row.append(elevation)
            values.append(elevation)
        width = max(width, len(parsed_row))
        rows.append(parsed_row)
    height = len(rows)
    if width == 0 or height == 0 or not values:
        raise DigitalTwinMissionEnvironmentError("GSI DEM response did not include elevations")
    padded: list[float | None] = []
    for row in rows:
        padded.extend(row)
        if len(row) < width:
            padded.extend([None] * (width - len(row)))
    no_data_ratio = round(1.0 - (len(values) / float(width * height)), 6)
    return tuple(padded), width, height, tuple(values), no_data_ratio


def _normalized_heightmap_samples_from_elevations(
    elevations: Sequence[float | None],
    *,
    elevation_min_m: float,
    elevation_max_m: float,
) -> tuple[float, ...]:
    vertical_scale = max(float(elevation_max_m) - float(elevation_min_m), 0.001)
    last_valid = 0.0
    normalized: list[float] = []
    for value in elevations:
        if value is None:
            normalized.append(last_valid)
            continue
        sample = min(1.0, max(0.0, (float(value) - float(elevation_min_m)) / vertical_scale))
        sample = round(sample, 6)
        normalized.append(sample)
        last_valid = sample
    return tuple(normalized)


def _source_backed_dem_heightmap_samples(
    *,
    tile_grids: Mapping[tuple[int, int, int], Mapping[str, Any]],
    bbox: Sequence[float],
    width: int = SOURCE_BACKED_HEIGHTMAP_SAMPLE_WIDTH,
    height: int = SOURCE_BACKED_HEIGHTMAP_SAMPLE_HEIGHT,
    zoom: int = 14,
) -> tuple[tuple[float, ...], tuple[float, ...], float]:
    lat_min, lon_min, lat_max, lon_max = (float(item) for item in bbox)
    sampled_elevations: list[float | None] = []
    valid_values: list[float] = []
    missing = 0
    for row in range(height):
        lat_ratio = row / float(height - 1)
        latitude = lat_min + (lat_max - lat_min) * lat_ratio
        for col in range(width):
            lon_ratio = col / float(width - 1)
            longitude = lon_min + (lon_max - lon_min) * lon_ratio
            _, tile_x_float, tile_y_float = _web_mercator_tile_float_for_wgs84(
                latitude,
                longitude,
                zoom=zoom,
            )
            tile_x = int(math.floor(tile_x_float))
            tile_y = int(math.floor(tile_y_float))
            tile = tile_grids.get((zoom, tile_x, tile_y))
            if not tile:
                sampled_elevations.append(None)
                missing += 1
                continue
            tile_width = int(tile["width"])
            tile_height = int(tile["height"])
            tile_values = tile["values"]
            tile_col = int(
                round(
                    min(1.0, max(0.0, tile_x_float - tile_x))
                    * float(tile_width - 1)
                )
            )
            tile_row = int(
                round(
                    min(1.0, max(0.0, tile_y_float - tile_y))
                    * float(tile_height - 1)
                )
            )
            elevation = tile_values[tile_row * tile_width + tile_col]
            sampled_elevations.append(elevation)
            if elevation is None:
                missing += 1
            else:
                valid_values.append(float(elevation))
    if not valid_values:
        raise DigitalTwinMissionEnvironmentError("source-backed DEM heightmap has no sampled elevations")
    elevation_min = min(valid_values)
    elevation_max = max(valid_values)
    normalized = _normalized_heightmap_samples_from_elevations(
        sampled_elevations,
        elevation_min_m=elevation_min,
        elevation_max_m=elevation_max,
    )
    no_data_ratio = round(missing / float(width * height), 6)
    return tuple(valid_values), normalized, no_data_ratio


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_vehicle_profile(
    profile_path: str | Path,
    *,
    profile_root: Path = VEHICLE_PROFILE_ROOT_ABS,
) -> tuple[dict[str, Any], str, str]:
    path = Path(profile_path)
    if not path.is_absolute():
        if path.name != str(path):
            raise DigitalTwinMissionEnvironmentError(
                "vehicle profile path must stay under repo-local vehicle profile root"
            )
        path = profile_root / path.name
    resolved = path.resolve()
    root = profile_root.resolve()
    if root not in resolved.parents:
        raise DigitalTwinMissionEnvironmentError(
            "vehicle profile path must stay under repo-local vehicle profile root"
        )
    if resolved.suffix.lower() != ".json":
        raise DigitalTwinMissionEnvironmentError("vehicle profile must be JSON")
    try:
        payload_bytes = resolved.read_bytes()
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigitalTwinMissionEnvironmentError(
            f"vehicle profile unavailable:{type(exc).__name__}"
        ) from exc
    return payload, str(resolved), sha256(payload_bytes).hexdigest()


class RealWorldMissionTarget(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION] = (
        REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION
    )
    target_id: str
    prompt_request_ref: str
    prompt_target: str
    resolved_location_label: str
    target_resolution_status: Literal[
        "prompt_target_unresolved",
        "prompt_target_ambiguous",
    ]
    confidence: float = Field(ge=0, le=1)
    provider: Literal["deterministic_prompt_parser"] = "deterministic_prompt_parser"
    source_url: str
    source_refs: tuple[str, ...]
    retrieved_at: datetime
    coordinate_frame: Literal["wgs84"] = "wgs84"
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude_m: float | None = Field(default=None, ge=-500, le=10000)
    requested_distance_km: float | None = Field(default=None, ge=0)
    requested_altitude_m: float | None = Field(default=None, ge=0, le=10000)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    bbox: tuple[float, float, float, float] | None = None
    sha256: str

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_source_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _coerce_retrieved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_target(self) -> "RealWorldMissionTarget":
        if not self.prompt_request_ref.startswith("px4_gazebo_mission_prompt_request:"):
            raise DigitalTwinMissionEnvironmentError(
                "real-world mission target requires prompt request ref"
            )
        if not self.source_refs:
            raise DigitalTwinMissionEnvironmentError(
                "real-world mission target requires source refs"
            )
        if self.source_url != STAGE1_TARGET_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 1 target resolution must remain prompt sourced"
            )
        return self


class RealWorldGeocodeCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION] = (
        REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION
    )
    candidate_id: str
    real_world_mission_target_ref: str
    source_query: str
    resolved_location_label: str
    candidate_status: Literal[
        "resolved_fixture_candidate",
        "ambiguous_target_requires_operator_selection",
    ]
    geocode_mode: Literal[
        "fixture_backed_target_resolution",
        "operator_confirmed_coordinate_pair",
    ] = "fixture_backed_target_resolution"
    provider: Literal[
        "digital_twin_fixture_geocoder",
        "operator_confirmed_wgs84",
    ] = "digital_twin_fixture_geocoder"
    source_url: str
    source_refs: tuple[str, ...]
    retrieved_at: datetime
    coordinate_frame: Literal["wgs84"] = "wgs84"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = Field(default=None, ge=-500, le=10000)
    horizontal_accuracy_m: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    bbox: tuple[float, float, float, float]
    geocode_hash: str
    sha256: str

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_source_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _coerce_retrieved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_candidate(self) -> "RealWorldGeocodeCandidate":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "geocode candidate requires target ref"
            )
        if self.real_world_mission_target_ref not in set(self.source_refs):
            raise DigitalTwinMissionEnvironmentError(
                "geocode candidate requires target source ref"
            )
        if self.geocode_mode == "fixture_backed_target_resolution" and (
            self.source_url != STAGE2_GEOCODE_SOURCE_URL
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 geocode candidate must remain fixture sourced"
            )
        if self.geocode_mode == "operator_confirmed_coordinate_pair" and (
            self.provider != "operator_confirmed_wgs84"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate-pair geocode candidate requires operator-confirmed provider"
            )
        if self.geocode_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "geocode candidate hash mismatch"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if not (lat_min <= self.latitude <= lat_max):
            raise DigitalTwinMissionEnvironmentError(
                "geocode candidate latitude must be inside bbox"
            )
        if not (lon_min <= self.longitude <= lon_max):
            raise DigitalTwinMissionEnvironmentError(
                "geocode candidate longitude must be inside bbox"
            )
        if (
            self.candidate_status == "resolved_fixture_candidate"
            and self.confidence < 0.5
        ):
            raise DigitalTwinMissionEnvironmentError(
                "resolved geocode candidate requires confidence"
            )
        return self


class RealWorldTargetResolution(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[REAL_WORLD_TARGET_RESOLUTION_SCHEMA_VERSION] = (
        REAL_WORLD_TARGET_RESOLUTION_SCHEMA_VERSION
    )
    resolution_id: str
    real_world_mission_target_ref: str
    source_refs: tuple[str, ...]
    target_resolution_status: Literal["source_backed_target_resolved"]
    provider: Literal["operator_confirmed_wgs84"] = "operator_confirmed_wgs84"
    source_url: str
    coordinate_frame: Literal["wgs84"] = "wgs84"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = Field(default=None, ge=-500, le=10000)
    bbox: tuple[float, float, float, float]
    horizontal_accuracy_m: float = Field(gt=0)
    source_backed_target: Literal[True] = True
    source_unavailable: Literal[False] = False
    target_resolution_hash: str
    sha256: str
    resolved_at: datetime

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_source_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("resolved_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_resolution(self) -> "RealWorldTargetResolution":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "target resolution requires mission target ref"
            )
        if self.real_world_mission_target_ref not in set(self.source_refs):
            raise DigitalTwinMissionEnvironmentError(
                "target resolution requires mission target source ref"
            )
        if self.source_url != SOURCE_BACKED_TARGET_RESOLUTION_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "source-backed target resolution requires operator-confirmed source URL"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "target resolution bbox range is invalid"
            )
        if not (lat_min <= self.latitude <= lat_max):
            raise DigitalTwinMissionEnvironmentError(
                "target resolution latitude must be inside bbox"
            )
        if not (lon_min <= self.longitude <= lon_max):
            raise DigitalTwinMissionEnvironmentError(
                "target resolution longitude must be inside bbox"
            )
        if self.target_resolution_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "target resolution hash mismatch"
            )
        return self


class TerrainDemSourceSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_DEM_SOURCE_SNAPSHOT_SCHEMA_VERSION] = (
        TERRAIN_DEM_SOURCE_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    real_world_target_resolution_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["gsi_elevation_tiles", "source_backed_dem_unavailable"]
    source_url: str
    dem_product: Literal["gsi_dem_txt"] = "gsi_dem_txt"
    snapshot_status: Literal[
        "source_backed_dem_captured",
        "blocked_source_unavailable",
    ]
    coordinate_frame: Literal["wgs84"] = "wgs84"
    tile_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0)
    elevation_min_m: float | None = None
    elevation_max_m: float | None = None
    elevation_mean_m: float | None = None
    no_data_ratio: float | None = Field(default=None, ge=0, le=1)
    heightmap_sample_source: str = ""
    heightmap_sample_width: int | None = None
    heightmap_sample_height: int | None = None
    heightmap_normalized_heights: tuple[float, ...] = ()
    heightmap_samples_sha256: str = ""
    provider_response_status: str
    source_backed_terrain: bool
    source_unavailable: bool
    terrain_hash: str
    sha256: str
    captured_at: datetime

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("heightmap_normalized_heights", mode="before")
    @classmethod
    def _coerce_float_tuple(cls, value: Any) -> tuple[float, ...]:
        return tuple(float(item) for item in (value or ()))

    @field_validator("captured_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "TerrainDemSourceSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "source DEM snapshot requires mission target ref"
            )
        if not self.real_world_target_resolution_ref.startswith(
            "real_world_target_resolution:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "source DEM snapshot requires target resolution ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_target_resolution_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "source DEM snapshot requires target and resolution refs"
            )
        if self.terrain_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError("source DEM hash mismatch")
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "source DEM snapshot bbox range is invalid"
            )
        if self.snapshot_status == "source_backed_dem_captured":
            if self.provider != "gsi_elevation_tiles":
                raise DigitalTwinMissionEnvironmentError(
                    "captured source DEM requires GSI provider"
                )
            if not self.source_url.startswith(SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "captured source DEM requires GSI DEM source URL"
                )
            if self.source_unavailable or not self.source_backed_terrain:
                raise DigitalTwinMissionEnvironmentError(
                    "captured source DEM must be source backed and available"
                )
            if not self.tile_refs:
                raise DigitalTwinMissionEnvironmentError(
                    "captured source DEM requires tile refs"
                )
            if (
                self.elevation_min_m is None
                or self.elevation_max_m is None
                or self.elevation_mean_m is None
                or self.no_data_ratio is None
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "captured source DEM requires elevation statistics"
                )
            if self.elevation_max_m < self.elevation_min_m:
                raise DigitalTwinMissionEnvironmentError(
                    "source DEM elevation range is invalid"
                )
            if not (
                self.elevation_min_m
                <= self.elevation_mean_m
                <= self.elevation_max_m
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "source DEM mean elevation must be inside range"
                )
            if self.heightmap_normalized_heights:
                if not self.heightmap_sample_width or not self.heightmap_sample_height:
                    raise DigitalTwinMissionEnvironmentError(
                        "captured source DEM heightmap samples require dimensions"
                    )
                if len(self.heightmap_normalized_heights) != (
                    self.heightmap_sample_width * self.heightmap_sample_height
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "captured source DEM heightmap sample count mismatch"
                    )
                expected_hash = sha256(
                    _canonical_json_bytes(
                        {"normalized_heights": self.heightmap_normalized_heights}
                    )
                ).hexdigest()
                if self.heightmap_samples_sha256 != expected_hash:
                    raise DigitalTwinMissionEnvironmentError(
                        "captured source DEM heightmap sample hash mismatch"
                    )
        else:
            if self.provider != "source_backed_dem_unavailable":
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source DEM requires unavailable provider marker"
                )
            if not self.source_unavailable or self.source_backed_terrain:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source DEM must mark source unavailable"
                )
            if self.tile_refs:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source DEM cannot include tile refs"
                )
        return self


class TerrainDemTileRequestCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION] = (
        TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION
    )
    request_id: str
    real_world_mission_target_ref: str
    real_world_geocode_candidate_ref: str
    real_world_target_resolution_ref: str = ""
    source_refs: tuple[str, ...]
    tile_request_status: Literal[
        "prepared_fixture_tile_request",
        "prepared_source_backed_tile_request",
        "blocked_by_ambiguous_geocode_candidate",
    ]
    provider: Literal[
        "digital_twin_fixture_dem_tile_index",
        "gsi_elevation_tile_index",
    ] = "digital_twin_fixture_dem_tile_index"
    source_url: str
    dem_product: Literal["stage2_fixture_dem_30m", "gsi_dem_txt"] = (
        "stage2_fixture_dem_30m"
    )
    request_mode: Literal[
        "fixture_backed_tile_index_lookup",
        "source_backed_gsi_tile_index_lookup",
    ] = "fixture_backed_tile_index_lookup"
    requested_coordinate_frame: Literal["wgs84"] = "wgs84"
    tile_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0)
    no_data_policy: Literal["defer_to_dem_snapshot"] = "defer_to_dem_snapshot"
    live_fetch_performed: bool = False
    terrain_snapshot_generated: Literal[False] = False
    heightmap_generated: Literal[False] = False
    request_hash: str
    sha256: str
    requested_at: datetime

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("requested_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_request(self) -> "TerrainDemTileRequestCandidate":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile request requires target ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile request requires geocode candidate ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_geocode_candidate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile request requires target and geocode source refs"
            )
        if self.provider == "digital_twin_fixture_dem_tile_index":
            if self.source_url != STAGE2_DEM_TILE_INDEX_SOURCE_URL:
                raise DigitalTwinMissionEnvironmentError(
                    "stage 2 DEM tile request must remain fixture sourced"
                )
            if self.tile_request_status not in {
                "prepared_fixture_tile_request",
                "blocked_by_ambiguous_geocode_candidate",
            }:
                raise DigitalTwinMissionEnvironmentError(
                    "fixture DEM tile request cannot claim source-backed status"
                )
            if (
                self.dem_product != "stage2_fixture_dem_30m"
                or self.request_mode != "fixture_backed_tile_index_lookup"
                or self.live_fetch_performed
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "fixture DEM tile request metadata mismatch"
                )
        else:
            if self.tile_request_status != "prepared_source_backed_tile_request":
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile request requires source-backed status"
                )
            if not self.real_world_target_resolution_ref.startswith(
                "real_world_target_resolution:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile request requires target resolution ref"
                )
            if self.real_world_target_resolution_ref not in set(self.source_refs):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile request requires target resolution source ref"
                )
            if not self.source_url.startswith(SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile request requires GSI DEM source URL"
                )
            if (
                self.dem_product != "gsi_dem_txt"
                or self.request_mode != "source_backed_gsi_tile_index_lookup"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile request metadata mismatch"
                )
        if self.request_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile request hash mismatch"
            )
        if (
            self.tile_request_status
            in {"prepared_fixture_tile_request", "prepared_source_backed_tile_request"}
            and not self.tile_refs
        ):
            raise DigitalTwinMissionEnvironmentError(
                "prepared DEM tile request requires tile refs"
            )
        if (
            self.tile_request_status == "blocked_by_ambiguous_geocode_candidate"
            and self.tile_refs
        ):
            raise DigitalTwinMissionEnvironmentError(
                "blocked DEM tile request cannot include tile refs"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile request bbox range is invalid"
            )
        return self


class TerrainDemTileSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION] = (
        TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    real_world_geocode_candidate_ref: str
    terrain_dem_tile_request_candidate_ref: str
    terrain_dem_source_snapshot_ref: str = ""
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_dem_tiles", "gsi_elevation_tiles"] = (
        "digital_twin_fixture_dem_tiles"
    )
    source_url: str
    snapshot_mode: Literal[
        "fixture_backed_dem_tile_snapshot",
        "source_backed_gsi_dem_tile_snapshot",
    ] = "fixture_backed_dem_tile_snapshot"
    coordinate_frame: Literal["wgs84"] = "wgs84"
    tile_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_mean_m: float
    no_data_ratio: float = Field(ge=0, le=1)
    heightmap_sample_source: str = ""
    heightmap_sample_width: int | None = None
    heightmap_sample_height: int | None = None
    heightmap_normalized_heights: tuple[float, ...] = ()
    heightmap_samples_sha256: str = ""
    terrain_hash: str
    sha256: str
    live_fetch_performed: bool = False
    heightmap_generated: Literal[False] = False
    digital_twin_world_generated: Literal[False] = False
    captured_at: datetime

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("heightmap_normalized_heights", mode="before")
    @classmethod
    def _coerce_float_tuple(cls, value: Any) -> tuple[float, ...]:
        return tuple(float(item) for item in (value or ()))

    @field_validator("captured_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "TerrainDemTileSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot requires target ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot requires geocode candidate ref"
            )
        if not self.terrain_dem_tile_request_candidate_ref.startswith(
            "terrain_dem_tile_request_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot requires DEM tile request ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_geocode_candidate_ref,
            self.terrain_dem_tile_request_candidate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot requires target, geocode, and request source refs"
            )
        if self.provider == "digital_twin_fixture_dem_tiles":
            if self.source_url != STAGE2_DEM_TILE_SNAPSHOT_SOURCE_URL:
                raise DigitalTwinMissionEnvironmentError(
                    "stage 2 DEM tile snapshot must remain fixture sourced"
                )
            if (
                self.snapshot_mode != "fixture_backed_dem_tile_snapshot"
                or self.live_fetch_performed
                or self.terrain_dem_source_snapshot_ref
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "fixture DEM tile snapshot metadata mismatch"
                )
        else:
            if not self.terrain_dem_source_snapshot_ref.startswith(
                "terrain_dem_source_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile snapshot requires source DEM snapshot ref"
                )
            if self.terrain_dem_source_snapshot_ref not in set(self.source_refs):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile snapshot requires source DEM source ref"
                )
            if not self.source_url.startswith(SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile snapshot requires GSI source URL"
                )
            if (
                self.snapshot_mode != "source_backed_gsi_dem_tile_snapshot"
                or not self.live_fetch_performed
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI DEM tile snapshot metadata mismatch"
                )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot requires tile refs"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot elevation range is invalid"
            )
        if not (self.elevation_min_m <= self.elevation_mean_m <= self.elevation_max_m):
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot mean elevation must be inside range"
            )
        if self.heightmap_normalized_heights:
            if not self.heightmap_sample_width or not self.heightmap_sample_height:
                raise DigitalTwinMissionEnvironmentError(
                    "DEM tile snapshot heightmap samples require dimensions"
                )
            if len(self.heightmap_normalized_heights) != (
                self.heightmap_sample_width * self.heightmap_sample_height
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "DEM tile snapshot heightmap sample count mismatch"
                )
            expected_hash = sha256(
                _canonical_json_bytes(
                    {"normalized_heights": self.heightmap_normalized_heights}
                )
            ).hexdigest()
            if self.heightmap_samples_sha256 != expected_hash:
                raise DigitalTwinMissionEnvironmentError(
                    "DEM tile snapshot heightmap sample hash mismatch"
                )
        if self.terrain_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot hash mismatch"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "DEM tile snapshot bbox range is invalid"
            )
        return self


class TerrainEnvironmentSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION] = (
        TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    provider: Literal["digital_twin_fixture_dem"] = "digital_twin_fixture_dem"
    source_url: str
    source_refs: tuple[str, ...]
    retrieved_at: datetime
    bbox: tuple[float, float, float, float]
    coordinate_frame: Literal["local_planning_grid"] = "local_planning_grid"
    tile_refs: tuple[str, ...]
    resolution_m: float = Field(gt=0)
    elevation_min_m: float
    elevation_max_m: float
    slope_risk_label: Literal["unknown", "low", "moderate", "high"]
    no_data_ratio: float = Field(ge=0, le=1)
    terrain_hash: str
    sha256: str
    snapshot_mode: Literal["prompt_projected_fixture"] = "prompt_projected_fixture"

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _coerce_retrieved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "TerrainEnvironmentSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "terrain snapshot requires target ref"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "terrain snapshot elevation range is invalid"
            )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "terrain snapshot requires tile refs"
            )
        if self.source_url != STAGE1_TERRAIN_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 1 terrain snapshot must remain fixture sourced"
            )
        if self.terrain_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "terrain snapshot hash mismatch"
            )
        return self


class TileBackedTerrainEnvironmentSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION] = (
        TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    real_world_geocode_candidate_ref: str
    terrain_dem_tile_snapshot_ref: str
    source_refs: tuple[str, ...]
    provider: Literal[
        "digital_twin_fixture_tile_backed_terrain",
        "gsi_elevation_tile_backed_terrain",
    ] = "digital_twin_fixture_tile_backed_terrain"
    source_url: str
    snapshot_mode: Literal[
        "tile_backed_fixture_terrain",
        "source_backed_gsi_tile_terrain",
    ] = "tile_backed_fixture_terrain"
    route_feasibility_binding_status: Literal[
        "not_bound",
        "bound_to_route_feasibility",
    ] = "not_bound"
    coordinate_frame: Literal["wgs84"] = "wgs84"
    tile_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_mean_m: float
    slope_risk_label: Literal["unknown", "low", "moderate", "high"]
    no_data_ratio: float = Field(ge=0, le=1)
    terrain_hash: str
    sha256: str
    live_fetch_performed: bool = False
    heightmap_generated: Literal[False] = False
    digital_twin_world_generated: Literal[False] = False
    captured_at: datetime

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("captured_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "TileBackedTerrainEnvironmentSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot requires target ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot requires geocode ref"
            )
        if not self.terrain_dem_tile_snapshot_ref.startswith(
            "terrain_dem_tile_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot requires DEM tile snapshot ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_geocode_candidate_ref,
            self.terrain_dem_tile_snapshot_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot requires target, geocode, and DEM tile source refs"
            )
        if self.provider == "digital_twin_fixture_tile_backed_terrain":
            if self.source_url != STAGE2_TILE_BACKED_TERRAIN_SOURCE_URL:
                raise DigitalTwinMissionEnvironmentError(
                    "stage 2 tile-backed terrain snapshot must remain fixture sourced"
                )
            if (
                self.snapshot_mode != "tile_backed_fixture_terrain"
                or self.live_fetch_performed
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "fixture tile-backed terrain metadata mismatch"
                )
        else:
            if not self.source_url.startswith(SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI tile-backed terrain requires GSI source URL"
                )
            if (
                self.snapshot_mode != "source_backed_gsi_tile_terrain"
                or not self.live_fetch_performed
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "GSI tile-backed terrain metadata mismatch"
                )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain elevation range is invalid"
            )
        if not (self.elevation_min_m <= self.elevation_mean_m <= self.elevation_max_m):
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain mean elevation must be inside range"
            )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot requires tile refs"
            )
        if self.terrain_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed terrain snapshot hash mismatch"
            )
        return self


class TerrainHeightmapCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_HEIGHTMAP_CANDIDATE_SCHEMA_VERSION] = (
        TERRAIN_HEIGHTMAP_CANDIDATE_SCHEMA_VERSION
    )
    candidate_id: str
    real_world_mission_target_ref: str
    real_world_geocode_candidate_ref: str
    terrain_dem_tile_snapshot_ref: str
    tile_backed_terrain_environment_snapshot_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_heightmap_candidate"] = (
        "digital_twin_fixture_heightmap_candidate"
    )
    source_url: str
    candidate_mode: Literal["fixture_tile_backed_heightmap_candidate"] = (
        "fixture_tile_backed_heightmap_candidate"
    )
    heightmap_status: Literal["candidate_generated"] = "candidate_generated"
    coordinate_frame: Literal["wgs84"] = "wgs84"
    height_encoding: Literal["normalized_float32_grid"] = "normalized_float32_grid"
    pixel_width: int = Field(gt=1)
    pixel_height: int = Field(gt=1)
    horizontal_resolution_m: float = Field(gt=0)
    vertical_scale_m: float = Field(ge=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_mean_m: float
    normalized_min: Literal[0.0] = 0.0
    normalized_max: Literal[1.0] = 1.0
    heightmap_sample_source: Literal[
        "synthetic_fixture_gradient",
        "source_dem_tile_samples",
    ] = "synthetic_fixture_gradient"
    heightmap_normalized_heights: tuple[float, ...] = ()
    heightmap_samples_sha256: str = ""
    tile_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    no_data_ratio: float = Field(ge=0, le=1)
    heightmap_hash: str
    sha256: str
    artifact_materialized: Literal[False] = False
    gazebo_world_generated: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False
    generated_at: datetime

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("heightmap_normalized_heights", mode="before")
    @classmethod
    def _coerce_float_tuple(cls, value: Any) -> tuple[float, ...]:
        return tuple(float(item) for item in (value or ()))

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_candidate(self) -> "TerrainHeightmapCandidate":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires target ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires geocode ref"
            )
        if not self.terrain_dem_tile_snapshot_ref.startswith(
            "terrain_dem_tile_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires DEM tile snapshot ref"
            )
        if not self.tile_backed_terrain_environment_snapshot_ref.startswith(
            "tile_backed_terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires tile-backed terrain ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_geocode_candidate_ref,
            self.terrain_dem_tile_snapshot_ref,
            self.tile_backed_terrain_environment_snapshot_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires target, geocode, DEM, and tile terrain source refs"
            )
        if self.source_url != STAGE2_HEIGHTMAP_CANDIDATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 heightmap candidate must remain fixture sourced"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate elevation range is invalid"
            )
        if not (self.elevation_min_m <= self.elevation_mean_m <= self.elevation_max_m):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate mean elevation must be inside range"
            )
        if self.vertical_scale_m != round(
            self.elevation_max_m - self.elevation_min_m,
            3,
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate vertical scale mismatch"
            )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate requires tile refs"
            )
        if self.heightmap_normalized_heights:
            if len(self.heightmap_normalized_heights) != (
                self.pixel_width * self.pixel_height
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "heightmap candidate sample count mismatch"
                )
            expected_hash = sha256(
                _canonical_json_bytes(
                    {"normalized_heights": self.heightmap_normalized_heights}
                )
            ).hexdigest()
            if self.heightmap_samples_sha256 != expected_hash:
                raise DigitalTwinMissionEnvironmentError(
                    "heightmap candidate sample hash mismatch"
                )
        if self.heightmap_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate hash mismatch"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap candidate bbox range is invalid"
            )
        return self


class TerrainHeightmapArtifact(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_HEIGHTMAP_ARTIFACT_SCHEMA_VERSION] = (
        TERRAIN_HEIGHTMAP_ARTIFACT_SCHEMA_VERSION
    )
    artifact_id: str
    heightmap_candidate_ref: str
    terrain_dem_tile_snapshot_ref: str
    tile_backed_terrain_environment_snapshot_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_heightmap_artifact"] = (
        "digital_twin_fixture_heightmap_artifact"
    )
    source_url: str
    artifact_status: Literal["materialized"] = "materialized"
    artifact_materialized: Literal[True] = True
    artifact_format: Literal["normalized_heightmap_grid_json"] = (
        "normalized_heightmap_grid_json"
    )
    encoding: Literal["row_major_normalized_float32"] = (
        "row_major_normalized_float32"
    )
    coordinate_frame: Literal["wgs84"] = "wgs84"
    pixel_width: int = Field(gt=1)
    pixel_height: int = Field(gt=1)
    horizontal_resolution_m: float = Field(gt=0)
    vertical_scale_m: float = Field(ge=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_mean_m: float
    normalized_min: Literal[0.0] = 0.0
    normalized_max: Literal[1.0] = 1.0
    bbox: tuple[float, float, float, float]
    tile_refs: tuple[str, ...]
    no_data_ratio: float = Field(ge=0, le=1)
    candidate_hash: str
    artifact_sha256: str
    sha256: str
    generated_at: datetime
    gazebo_world_generated: Literal[False] = False
    coordinate_transform_generated: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_artifact(self) -> "TerrainHeightmapArtifact":
        if not self.heightmap_candidate_ref.startswith("terrain_heightmap_candidate:"):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires candidate ref"
            )
        if not self.terrain_dem_tile_snapshot_ref.startswith(
            "terrain_dem_tile_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires DEM tile snapshot ref"
            )
        if not self.tile_backed_terrain_environment_snapshot_ref.startswith(
            "tile_backed_terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires tile-backed terrain ref"
            )
        required_refs = {
            self.heightmap_candidate_ref,
            self.terrain_dem_tile_snapshot_ref,
            self.tile_backed_terrain_environment_snapshot_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires candidate, DEM, and tile terrain source refs"
            )
        if self.source_url != STAGE2_HEIGHTMAP_ARTIFACT_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 heightmap artifact must remain fixture sourced"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact elevation range is invalid"
            )
        if not (self.elevation_min_m <= self.elevation_mean_m <= self.elevation_max_m):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact mean elevation must be inside range"
            )
        if self.vertical_scale_m != round(
            self.elevation_max_m - self.elevation_min_m,
            3,
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact vertical scale mismatch"
            )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires tile refs"
            )
        if not self.candidate_hash:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact requires candidate hash"
            )
        if self.artifact_sha256 != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact hash mismatch"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap artifact bbox range is invalid"
            )
        return self


class TerrainHeightmapFileArtifact(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TERRAIN_HEIGHTMAP_FILE_ARTIFACT_SCHEMA_VERSION] = (
        TERRAIN_HEIGHTMAP_FILE_ARTIFACT_SCHEMA_VERSION
    )
    file_artifact_id: str
    terrain_heightmap_artifact_ref: str
    terrain_heightmap_candidate_ref: str
    terrain_dem_tile_snapshot_ref: str
    tile_backed_terrain_environment_snapshot_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_heightmap_file_artifact"] = (
        "digital_twin_fixture_heightmap_file_artifact"
    )
    source_url: str
    file_artifact_status: Literal["materialized"] = "materialized"
    file_materialized: Literal[True] = True
    file_format: Literal["normalized_heightmap_grid_json"] = (
        "normalized_heightmap_grid_json"
    )
    encoding: Literal["row_major_normalized_float32"] = (
        "row_major_normalized_float32"
    )
    gazebo_dem_file_format: Literal["portable_graymap_p5"] = "portable_graymap_p5"
    gazebo_dem_encoding: Literal["uint8_grayscale_heightmap"] = (
        "uint8_grayscale_heightmap"
    )
    coordinate_frame: Literal["wgs84"] = "wgs84"
    pixel_width: int = Field(gt=1)
    pixel_height: int = Field(gt=1)
    horizontal_resolution_m: float = Field(gt=0)
    vertical_scale_m: float = Field(ge=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_mean_m: float
    normalized_min: Literal[0.0] = 0.0
    normalized_max: Literal[1.0] = 1.0
    bbox: tuple[float, float, float, float]
    tile_refs: tuple[str, ...]
    no_data_ratio: float = Field(ge=0, le=1)
    artifact_sha256: str
    candidate_hash: str
    file_sha256: str
    sha256: str
    file_path_or_artifact_uri: str
    gazebo_dem_file_sha256: str
    gazebo_dem_file_path_or_artifact_uri: str
    generated_at: datetime
    gazebo_world_generated: Literal[False] = False
    coordinate_transform_generated: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False

    @field_validator("source_refs", "tile_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_file_artifact(self) -> "TerrainHeightmapFileArtifact":
        if not self.terrain_heightmap_artifact_ref.startswith(
            "terrain_heightmap_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires heightmap artifact ref"
            )
        if not self.terrain_heightmap_candidate_ref.startswith(
            "terrain_heightmap_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires candidate ref"
            )
        if not self.terrain_dem_tile_snapshot_ref.startswith(
            "terrain_dem_tile_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires DEM tile snapshot ref"
            )
        if not self.tile_backed_terrain_environment_snapshot_ref.startswith(
            "tile_backed_terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires tile-backed terrain ref"
            )
        required_refs = {
            self.terrain_heightmap_artifact_ref,
            self.terrain_heightmap_candidate_ref,
            self.terrain_dem_tile_snapshot_ref,
            self.tile_backed_terrain_environment_snapshot_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires artifact, candidate, DEM, and tile terrain source refs"
            )
        if self.source_url != STAGE2_HEIGHTMAP_FILE_ARTIFACT_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 heightmap file artifact must remain fixture sourced"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact elevation range is invalid"
            )
        if not (self.elevation_min_m <= self.elevation_mean_m <= self.elevation_max_m):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact mean elevation must be inside range"
            )
        if self.vertical_scale_m != round(
            self.elevation_max_m - self.elevation_min_m,
            3,
        ):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact vertical scale mismatch"
            )
        if not self.tile_refs:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires tile refs"
            )
        if not self.artifact_sha256:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires artifact hash"
            )
        if not self.candidate_hash:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires candidate hash"
            )
        if self.file_sha256 != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact hash mismatch"
            )
        if not self.file_path_or_artifact_uri:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires file path or artifact URI"
            )
        normalized_path = self.file_path_or_artifact_uri.replace("\\", "/")
        expected_prefix = str(HEIGHTMAP_FILE_ARTIFACT_ROOT).replace("\\", "/") + "/"
        if not normalized_path.startswith(expected_prefix):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact path must stay under output/digital_twin/heightmaps"
            )
        if not normalized_path.endswith(".heightmap.json"):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact path must end with .heightmap.json"
            )
        if not self.gazebo_dem_file_sha256:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact requires Gazebo DEM file hash"
            )
        dem_path = self.gazebo_dem_file_path_or_artifact_uri.replace("\\", "/")
        if not dem_path.startswith(expected_prefix):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap Gazebo DEM file path must stay under output/digital_twin/heightmaps"
            )
        if not dem_path.endswith(".heightmap.pgm"):
            raise DigitalTwinMissionEnvironmentError(
                "heightmap Gazebo DEM file path must end with .heightmap.pgm"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "heightmap file artifact bbox range is invalid"
            )
        return self


class GazeboWorldCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION] = (
        GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION
    )
    world_candidate_id: str
    terrain_heightmap_file_artifact_ref: str
    terrain_heightmap_artifact_ref: str
    terrain_heightmap_candidate_ref: str
    digital_twin_route_plan_ref: str
    weather_environment_policy_gate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_gazebo_world_candidate"] = (
        "digital_twin_fixture_gazebo_world_candidate"
    )
    source_url: str
    world_candidate_status: Literal["generated_for_planning_only"] = (
        "generated_for_planning_only"
    )
    world_format: Literal["gz_sim_world_candidate"] = "gz_sim_world_candidate"
    heightmap_uri: str
    file_sha256: str
    terrain_scale: tuple[float, float, float]
    vertical_scale_m: float = Field(ge=0)
    bbox: tuple[float, float, float, float]
    coordinate_frame: Literal["wgs84"] = "wgs84"
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    weather_policy_gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    execution_binding_allowed: Literal[False] = False
    gazebo_world_materialized: Literal[False] = False
    coordinate_transform_generated: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    world_candidate_sha256: str
    sha256: str
    generated_at: datetime

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @field_validator("terrain_scale", mode="before")
    @classmethod
    def _coerce_terrain_scale(cls, value: Any) -> tuple[float, float, float]:
        if len(value) != 3:
            raise ValueError("terrain_scale requires three values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_world_candidate(self) -> "GazeboWorldCandidate":
        if not self.terrain_heightmap_file_artifact_ref.startswith(
            "terrain_heightmap_file_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires heightmap file artifact ref"
            )
        if not self.terrain_heightmap_artifact_ref.startswith(
            "terrain_heightmap_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires heightmap artifact ref"
            )
        if not self.terrain_heightmap_candidate_ref.startswith(
            "terrain_heightmap_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires heightmap candidate ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires route plan ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires weather policy gate ref"
            )
        required_refs = {
            self.terrain_heightmap_file_artifact_ref,
            self.terrain_heightmap_artifact_ref,
            self.terrain_heightmap_candidate_ref,
            self.digital_twin_route_plan_ref,
            self.weather_environment_policy_gate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires file, artifact, candidate, route plan, and weather gate source refs"
            )
        if self.source_url != STAGE2_GAZEBO_WORLD_CANDIDATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 gazebo world candidate must remain fixture sourced"
            )
        if not self.heightmap_uri:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires heightmap URI"
            )
        if not self.file_sha256:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate requires heightmap file hash"
            )
        if any(item <= 0 for item in self.terrain_scale):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate terrain scale must be positive"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate bbox range is invalid"
            )
        if self.world_candidate_sha256 != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world candidate hash mismatch"
            )
        return self


class GazeboWorldArtifact(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_WORLD_ARTIFACT_SCHEMA_VERSION] = (
        GAZEBO_WORLD_ARTIFACT_SCHEMA_VERSION
    )
    world_artifact_id: str
    gazebo_world_candidate_ref: str
    terrain_heightmap_file_artifact_ref: str
    terrain_heightmap_artifact_ref: str
    terrain_heightmap_candidate_ref: str
    digital_twin_route_plan_ref: str
    weather_environment_policy_gate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_gazebo_world_artifact"] = (
        "digital_twin_fixture_gazebo_world_artifact"
    )
    source_url: str
    world_artifact_status: Literal["materialized"] = "materialized"
    world_format: Literal["gz_sim_sdf_world"] = "gz_sim_sdf_world"
    world_file_path_or_artifact_uri: str
    world_file_sha256: str
    heightmap_uri: str
    heightmap_file_sha256: str
    terrain_scale: tuple[float, float, float]
    vertical_scale_m: float = Field(ge=0)
    bbox: tuple[float, float, float, float]
    coordinate_frame: Literal["wgs84"] = "wgs84"
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    weather_policy_gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    execution_binding_allowed: Literal[False] = False
    gazebo_world_materialized: Literal[True] = True
    coordinate_transform_generated: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    sha256: str
    generated_at: datetime

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @field_validator("terrain_scale", mode="before")
    @classmethod
    def _coerce_terrain_scale(cls, value: Any) -> tuple[float, float, float]:
        if len(value) != 3:
            raise ValueError("terrain_scale requires three values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_world_artifact(self) -> "GazeboWorldArtifact":
        if not self.gazebo_world_candidate_ref.startswith("gazebo_world_candidate:"):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires world candidate ref"
            )
        if not self.terrain_heightmap_file_artifact_ref.startswith(
            "terrain_heightmap_file_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires heightmap file artifact ref"
            )
        if not self.terrain_heightmap_artifact_ref.startswith(
            "terrain_heightmap_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires heightmap artifact ref"
            )
        if not self.terrain_heightmap_candidate_ref.startswith(
            "terrain_heightmap_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires heightmap candidate ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires route plan ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires weather policy gate ref"
            )
        required_refs = {
            self.gazebo_world_candidate_ref,
            self.terrain_heightmap_file_artifact_ref,
            self.terrain_heightmap_artifact_ref,
            self.terrain_heightmap_candidate_ref,
            self.digital_twin_route_plan_ref,
            self.weather_environment_policy_gate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires world candidate, heightmap, route plan, and weather gate source refs"
            )
        if self.source_url != STAGE2_GAZEBO_WORLD_ARTIFACT_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 gazebo world artifact must remain fixture sourced"
            )
        if not self.world_file_path_or_artifact_uri:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires world file path or artifact URI"
            )
        normalized_path = self.world_file_path_or_artifact_uri.replace("\\", "/")
        expected_prefix = str(GAZEBO_WORLD_ARTIFACT_ROOT).replace("\\", "/") + "/"
        if not normalized_path.startswith(expected_prefix):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact path must stay under output/digital_twin/worlds"
            )
        if not normalized_path.endswith(".world.sdf"):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact path must end with .world.sdf"
            )
        if self.world_file_sha256 != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact hash mismatch"
            )
        if not self.heightmap_uri:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires heightmap URI"
            )
        if not self.heightmap_file_sha256:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact requires heightmap file hash"
            )
        if any(item <= 0 for item in self.terrain_scale):
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact terrain scale must be positive"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "gazebo world artifact bbox range is invalid"
            )
        return self


class CoordinateTransformCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION] = (
        COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION
    )
    transform_candidate_id: str
    gazebo_world_artifact_ref: str
    gazebo_world_candidate_ref: str
    terrain_heightmap_file_artifact_ref: str
    digital_twin_route_plan_ref: str
    real_world_geocode_candidate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_coordinate_transform_candidate"] = (
        "digital_twin_fixture_coordinate_transform_candidate"
    )
    source_url: str
    transform_candidate_status: Literal["candidate_generated"] = (
        "candidate_generated"
    )
    coordinate_frame_source: Literal["wgs84"] = "wgs84"
    coordinate_frame_target: Literal["gazebo_world_local"] = "gazebo_world_local"
    origin_latitude: float = Field(ge=-90, le=90)
    origin_longitude: float = Field(ge=-180, le=180)
    origin_altitude_m: float = Field(ge=-500, le=10000)
    world_origin_x_m: float
    world_origin_y_m: float
    world_origin_z_m: float
    meters_per_degree_lat: float = Field(gt=0)
    meters_per_degree_lon: float = Field(gt=0)
    terrain_scale: tuple[float, float, float]
    bbox: tuple[float, float, float, float]
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    gazebo_world_materialized: Literal[True] = True
    coordinate_transform_materialized: Literal[False] = False
    execution_binding_allowed: Literal[False] = False
    px4_mission_items_generated: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    transform_hash: str
    sha256: str
    generated_at: datetime

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("bbox", mode="before")
    @classmethod
    def _coerce_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if len(value) != 4:
            raise ValueError("bbox requires four values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @field_validator("terrain_scale", mode="before")
    @classmethod
    def _coerce_terrain_scale(cls, value: Any) -> tuple[float, float, float]:
        if len(value) != 3:
            raise ValueError("terrain_scale requires three values")
        return tuple(float(item) for item in value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _validate_transform_candidate(self) -> "CoordinateTransformCandidate":
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires world artifact ref"
            )
        if not self.gazebo_world_candidate_ref.startswith("gazebo_world_candidate:"):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires world candidate ref"
            )
        if not self.terrain_heightmap_file_artifact_ref.startswith(
            "terrain_heightmap_file_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires heightmap file artifact ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires route plan ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires geocode candidate ref"
            )
        required_refs = {
            self.gazebo_world_artifact_ref,
            self.gazebo_world_candidate_ref,
            self.terrain_heightmap_file_artifact_ref,
            self.digital_twin_route_plan_ref,
            self.real_world_geocode_candidate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate requires world, heightmap, route plan, and geocode source refs"
            )
        if self.source_url != STAGE2_COORDINATE_TRANSFORM_CANDIDATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 coordinate transform candidate must remain fixture sourced"
            )
        if any(item <= 0 for item in self.terrain_scale):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate terrain scale must be positive"
            )
        lat_min, lon_min, lat_max, lon_max = self.bbox
        if lat_min > lat_max or lon_min > lon_max:
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate bbox range is invalid"
            )
        if not (lat_min <= self.origin_latitude <= lat_max):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform origin latitude must be inside bbox"
            )
        if not (lon_min <= self.origin_longitude <= lon_max):
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform origin longitude must be inside bbox"
            )
        if self.transform_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "coordinate transform candidate hash mismatch"
            )
        return self


class WeatherEnvironmentSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION] = (
        WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    weather_source_snapshot_ref: str = ""
    provider: Literal[
        "deterministic_prompt_weather_parser",
        "open_meteo_jma_weather",
        "source_backed_weather_unavailable",
    ] = "deterministic_prompt_weather_parser"
    source_url: str
    source_refs: tuple[str, ...]
    retrieved_at: datetime
    valid_at: datetime
    location_label: str
    weather_hash: str
    sha256: str
    forecast_or_observed: Literal[
        "operator_prompt_constraint",
        "open_meteo_current_conditions",
        "source_weather_unavailable",
    ] = "operator_prompt_constraint"
    rain_or_precipitation: bool
    precipitation_label: Literal["none", "rain_or_storm"]
    precipitation_mm_per_hour: float | None = Field(default=None, ge=0)
    wind_speed_mps: float | None = Field(default=None, ge=0)
    wind_gust_mps: float | None = Field(default=None, ge=0)
    wind_direction_deg: float | None = Field(default=None, ge=0, le=360)
    visibility_m: float | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    pressure_hpa: float | None = Field(default=None, gt=0)
    source_unavailable: bool = False
    stale_or_missing_external_weather: bool = True

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_source_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("retrieved_at", "valid_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "WeatherEnvironmentSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather snapshot requires target ref"
            )
        if self.rain_or_precipitation and self.precipitation_label == "none":
            raise DigitalTwinMissionEnvironmentError(
                "weather snapshot precipitation label mismatch"
            )
        if self.provider == "deterministic_prompt_weather_parser":
            if self.source_url != STAGE1_WEATHER_SOURCE_URL:
                raise DigitalTwinMissionEnvironmentError(
                    "stage 1 weather snapshot must remain prompt sourced"
                )
            if (
                self.weather_source_snapshot_ref
                or self.forecast_or_observed != "operator_prompt_constraint"
                or not self.stale_or_missing_external_weather
                or self.source_unavailable
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "prompt weather snapshot metadata mismatch"
                )
        elif self.provider == "open_meteo_jma_weather":
            if not self.weather_source_snapshot_ref.startswith(
                "weather_source_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "source-backed weather snapshot requires weather source ref"
                )
            if not self.source_url.startswith(SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "source-backed weather snapshot requires Open-Meteo source URL"
                )
            if (
                self.forecast_or_observed != "open_meteo_current_conditions"
                or self.stale_or_missing_external_weather
                or self.source_unavailable
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "source-backed weather snapshot metadata mismatch"
                )
        else:
            if not self.weather_source_snapshot_ref.startswith(
                "weather_source_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source weather snapshot requires weather source ref"
                )
            if (
                self.forecast_or_observed != "source_weather_unavailable"
                or not self.stale_or_missing_external_weather
                or not self.source_unavailable
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source weather snapshot metadata mismatch"
                )
        if self.weather_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "weather snapshot hash mismatch"
            )
        return self


class WeatherSourceSnapshot(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[WEATHER_SOURCE_SNAPSHOT_SCHEMA_VERSION] = (
        WEATHER_SOURCE_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    real_world_mission_target_ref: str
    real_world_target_resolution_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["open_meteo_jma", "source_backed_weather_unavailable"]
    source_url: str
    snapshot_status: Literal[
        "source_backed_weather_captured",
        "blocked_source_unavailable",
    ]
    coordinate_frame: Literal["wgs84"] = "wgs84"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    valid_at: datetime | None = None
    captured_at: datetime
    precipitation_mm_per_hour: float | None = Field(default=None, ge=0)
    wind_speed_mps: float | None = Field(default=None, ge=0)
    wind_gust_mps: float | None = Field(default=None, ge=0)
    wind_direction_deg: float | None = Field(default=None, ge=0, le=360)
    visibility_m: float | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    pressure_hpa: float | None = Field(default=None, gt=0)
    provider_response_status: str
    source_backed_weather: bool
    source_unavailable: bool
    weather_hash: str
    sha256: str

    @field_validator("source_refs", mode="before")
    @classmethod
    def _coerce_source_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("captured_at", "valid_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_source_snapshot(self) -> "WeatherSourceSnapshot":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "source weather snapshot requires mission target ref"
            )
        if not self.real_world_target_resolution_ref.startswith(
            "real_world_target_resolution:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "source weather snapshot requires target resolution ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.real_world_target_resolution_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "source weather snapshot requires target and resolution refs"
            )
        if self.weather_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "source weather hash mismatch"
            )
        if self.snapshot_status == "source_backed_weather_captured":
            if self.provider != "open_meteo_jma":
                raise DigitalTwinMissionEnvironmentError(
                    "captured source weather requires Open-Meteo JMA provider"
                )
            if not self.source_url.startswith(SOURCE_BACKED_OPEN_METEO_JMA_URL_PREFIX):
                raise DigitalTwinMissionEnvironmentError(
                    "captured source weather requires Open-Meteo JMA source URL"
                )
            if self.source_unavailable or not self.source_backed_weather:
                raise DigitalTwinMissionEnvironmentError(
                    "captured source weather must be source backed and available"
                )
            if self.valid_at is None:
                raise DigitalTwinMissionEnvironmentError(
                    "captured source weather requires valid_at"
                )
        else:
            if self.provider != "source_backed_weather_unavailable":
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source weather requires unavailable provider marker"
                )
            if self.valid_at is not None:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source weather cannot include valid_at"
                )
            if not self.source_unavailable or self.source_backed_weather:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked source weather must mark source unavailable"
                )
        return self


class DigitalTwinRouteFeasibility(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION] = (
        DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION
    )
    feasibility_id: str
    real_world_mission_target_ref: str
    terrain_environment_snapshot_ref: str
    prompt_projected_terrain_environment_snapshot_ref: str
    tile_backed_terrain_environment_snapshot_ref: str
    weather_environment_snapshot_ref: str
    source_refs: tuple[str, ...]
    route_feasibility_input_source: Literal[
        "prompt_projected_terrain",
        "tile_backed_terrain",
    ]
    route_feasibility_status: Literal[
        "feasible_for_planning",
        "feasible_with_warnings",
        "blocked_for_planning",
    ]
    requested_distance_m: float = Field(gt=0)
    actual_route_distance_m: float = Field(gt=0)
    elevation_min_m: float
    elevation_max_m: float
    elevation_gain_m: float = Field(ge=0)
    average_slope_percent: float = Field(ge=0)
    max_projected_slope_percent: float = Field(ge=0)
    min_terrain_clearance_m: float = Field(ge=0)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    rain_or_precipitation: bool
    battery_margin_assumption_percent: float = Field(ge=0, le=100)
    terrain_risk_label: Literal["unknown", "low", "moderate", "high"]
    route_risk_labels: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    feasibility_hash: str
    sha256: str
    computed_at: datetime

    @field_validator(
        "source_refs",
        "route_risk_labels",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("computed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_feasibility(self) -> "DigitalTwinRouteFeasibility":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility requires target ref"
            )
        if self.route_feasibility_input_source == "tile_backed_terrain":
            if not self.terrain_environment_snapshot_ref.startswith(
                "tile_backed_terrain_environment_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route feasibility requires tile-backed terrain ref"
                )
            if (
                self.tile_backed_terrain_environment_snapshot_ref
                != self.terrain_environment_snapshot_ref
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route feasibility terrain ref mismatch"
                )
            if not self.prompt_projected_terrain_environment_snapshot_ref.startswith(
                "terrain_environment_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route feasibility requires prompt-projected terrain ref"
                )
        elif not self.terrain_environment_snapshot_ref.startswith(
            "terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility requires terrain snapshot ref"
            )
        if self.route_feasibility_input_source == "prompt_projected_terrain":
            if (
                self.prompt_projected_terrain_environment_snapshot_ref
                != self.terrain_environment_snapshot_ref
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "prompt-projected route feasibility terrain ref mismatch"
                )
            if self.tile_backed_terrain_environment_snapshot_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "prompt-projected route feasibility cannot bind tile-backed terrain"
                )
        if not self.weather_environment_snapshot_ref.startswith(
            "weather_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility requires weather snapshot ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.terrain_environment_snapshot_ref,
            self.prompt_projected_terrain_environment_snapshot_ref,
            self.weather_environment_snapshot_ref,
        }
        if self.tile_backed_terrain_environment_snapshot_ref:
            required_refs.add(self.tile_backed_terrain_environment_snapshot_ref)
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility requires source refs for target, terrain inputs, and weather"
            )
        if self.elevation_max_m < self.elevation_min_m:
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility elevation range is invalid"
            )
        if self.feasibility_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility hash mismatch"
            )
        if self.route_feasibility_status == "blocked_for_planning" and not (
            self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "blocked route feasibility requires blocked reasons"
            )
        if self.route_feasibility_status != "blocked_for_planning" and (
            self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "non-blocked route feasibility cannot include blocked reasons"
            )
        if self.route_feasibility_status == "feasible_with_warnings" and not (
            self.warning_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "warning route feasibility requires warning reasons"
            )
        return self


class WeatherEnvironmentPolicyGate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION] = (
        WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION
    )
    gate_id: str
    weather_environment_snapshot_ref: str
    digital_twin_route_feasibility_ref: str
    source_refs: tuple[str, ...]
    gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    operator_escalation_required: bool
    rain_or_precipitation: bool
    external_weather_required: bool
    external_weather_observed: bool
    max_precipitation_mm_per_hour: float = Field(ge=0)
    min_visibility_m: float = Field(ge=0)
    max_wind_speed_mps: float = Field(ge=0)
    policy_risk_labels: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    gate_hash: str
    sha256: str
    evaluated_at: datetime

    @field_validator(
        "source_refs",
        "policy_risk_labels",
        "warning_reasons",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_gate(self) -> "WeatherEnvironmentPolicyGate":
        if not self.weather_environment_snapshot_ref.startswith(
            "weather_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather policy gate requires weather snapshot ref"
            )
        if not self.digital_twin_route_feasibility_ref.startswith(
            "digital_twin_route_feasibility:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather policy gate requires route feasibility ref"
            )
        required_refs = {
            self.weather_environment_snapshot_ref,
            self.digital_twin_route_feasibility_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "weather policy gate requires source refs for weather and route"
            )
        if self.gate_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "weather policy gate hash mismatch"
            )
        if self.gate_status == "blocked_for_planning" and not self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "blocked weather policy gate requires blocked reasons"
            )
        if self.gate_status != "blocked_for_planning" and self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "non-blocked weather policy gate cannot include blocked reasons"
            )
        if (
            self.gate_status == "blocked_for_planning"
            and not self.operator_escalation_required
        ):
            raise DigitalTwinMissionEnvironmentError(
                "blocked weather policy gate requires operator escalation"
            )
        if self.operator_escalation_required and self.gate_status != (
            "blocked_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "operator escalation requires blocked weather policy gate"
            )
        if self.gate_status == "warning_for_planning" and not self.warning_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "warning weather policy gate requires warning reasons"
            )
        if self.gate_status == "passed_for_planning" and self.warning_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "passed weather policy gate cannot include warning reasons"
            )
        return self


class VehicleFlightEnvelope(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[VEHICLE_FLIGHT_ENVELOPE_SCHEMA_VERSION] = (
        VEHICLE_FLIGHT_ENVELOPE_SCHEMA_VERSION
    )
    envelope_id: str
    real_world_mission_target_ref: str
    weather_environment_policy_gate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["repo_local_vehicle_profile"] = "repo_local_vehicle_profile"
    source_url: str
    vehicle_profile_ref: str
    vehicle_profile_path: str
    vehicle_profile_sha256: str
    vehicle_id: str
    profile_version: str
    envelope_status: Literal["passed", "blocked"]
    max_payload_kg: float = Field(gt=0)
    requested_payload_kg: float = Field(ge=0)
    max_range_m: float = Field(gt=0)
    requested_route_distance_m: float = Field(gt=0)
    max_takeoff_altitude_m: float = Field(gt=0)
    target_altitude_m: float | None = Field(default=None, ge=0)
    max_wind_speed_mps: float = Field(gt=0)
    observed_wind_speed_mps: float | None = Field(default=None, ge=0)
    nominal_battery_wh: float = Field(gt=0)
    reserve_percent: float = Field(ge=0, le=100)
    cruise_energy_wh_per_km: float = Field(gt=0)
    climb_energy_wh_per_100m: float = Field(ge=0)
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...] = ()
    envelope_hash: str
    sha256: str
    evaluated_at: datetime

    @field_validator("source_refs", "blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_envelope(self) -> "VehicleFlightEnvelope":
        if not self.real_world_mission_target_ref.startswith("real_world_mission_target:"):
            raise DigitalTwinMissionEnvironmentError("vehicle envelope requires target ref")
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError("vehicle envelope requires weather gate ref")
        if not {self.real_world_mission_target_ref, self.weather_environment_policy_gate_ref}.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "vehicle envelope requires target and weather gate source refs"
            )
        if self.envelope_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError("vehicle envelope hash mismatch")
        if self.envelope_status == "blocked" and not self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("blocked vehicle envelope requires reasons")
        if self.envelope_status == "passed" and self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("passed vehicle envelope cannot include blocked reasons")
        if self.requested_payload_kg > self.max_payload_kg and "payload_over_limit" not in self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("payload over limit must block vehicle envelope")
        if self.requested_route_distance_m > self.max_range_m and "range_over_limit" not in self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("range over limit must block vehicle envelope")
        if (
            self.target_altitude_m is not None
            and self.target_altitude_m > self.max_takeoff_altitude_m
            and "altitude_over_limit" not in self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError("altitude over limit must block vehicle envelope")
        if (
            self.observed_wind_speed_mps is not None
            and self.observed_wind_speed_mps > self.max_wind_speed_mps
            and "wind_over_limit" not in self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError("wind over limit must block vehicle envelope")
        return self


class MissionEnergyBudget(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MISSION_ENERGY_BUDGET_SCHEMA_VERSION] = (
        MISSION_ENERGY_BUDGET_SCHEMA_VERSION
    )
    budget_id: str
    vehicle_flight_envelope_ref: str
    digital_twin_route_feasibility_ref: str
    weather_environment_policy_gate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["deterministic_vehicle_energy_budget"] = (
        "deterministic_vehicle_energy_budget"
    )
    source_url: str
    budget_status: Literal["passed", "blocked"]
    available_battery_wh: float = Field(gt=0)
    reserve_battery_wh: float = Field(ge=0)
    cruise_energy_wh: float = Field(ge=0)
    climb_energy_wh: float = Field(ge=0)
    payload_energy_margin_wh: float = Field(ge=0)
    wind_energy_margin_wh: float = Field(ge=0)
    required_energy_wh: float = Field(ge=0)
    remaining_energy_wh: float
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...] = ()
    budget_hash: str
    sha256: str
    computed_at: datetime

    @field_validator("source_refs", "blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("computed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_budget(self) -> "MissionEnergyBudget":
        if not self.vehicle_flight_envelope_ref.startswith("vehicle_flight_envelope:"):
            raise DigitalTwinMissionEnvironmentError("energy budget requires vehicle envelope ref")
        if not self.digital_twin_route_feasibility_ref.startswith("digital_twin_route_feasibility:"):
            raise DigitalTwinMissionEnvironmentError("energy budget requires route feasibility ref")
        if not self.weather_environment_policy_gate_ref.startswith("weather_environment_policy_gate:"):
            raise DigitalTwinMissionEnvironmentError("energy budget requires weather gate ref")
        required_refs = {
            self.vehicle_flight_envelope_ref,
            self.digital_twin_route_feasibility_ref,
            self.weather_environment_policy_gate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "energy budget requires vehicle, route, and weather source refs"
            )
        if self.budget_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError("energy budget hash mismatch")
        if self.budget_status == "blocked" and not self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("blocked energy budget requires reasons")
        if self.budget_status == "passed" and self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError("passed energy budget cannot include blocked reasons")
        return self


class DigitalTwinRoutePlan(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION] = (
        DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION
    )
    route_plan_id: str
    real_world_mission_target_ref: str
    terrain_environment_snapshot_ref: str
    prompt_projected_terrain_environment_snapshot_ref: str
    tile_backed_terrain_environment_snapshot_ref: str
    weather_environment_snapshot_ref: str
    digital_twin_route_feasibility_ref: str
    weather_environment_policy_gate_ref: str
    source_refs: tuple[str, ...]
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    route_plan_mode: Literal[
        "stage1_projected_planning_route",
        "stage2_tile_backed_planning_route",
    ] = "stage1_projected_planning_route"
    source_projection_kind: Literal[
        "prompt_fixture_projection",
        "fixture_tile_backed_terrain_projection",
    ] = "prompt_fixture_projection"
    route_feasibility_input_source: Literal[
        "prompt_projected_terrain",
        "tile_backed_terrain",
    ]
    route_feasibility_status: str
    weather_policy_gate_status: str
    operator_escalation_required: bool
    requested_distance_m: float = Field(gt=0)
    planned_route_distance_m: float = Field(gt=0)
    elevation_gain_m: float = Field(ge=0)
    average_slope_percent: float = Field(ge=0)
    max_projected_slope_percent: float = Field(ge=0)
    terrain_clearance_min_m: float = Field(ge=0)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    rain_or_precipitation: bool
    digital_twin_world_generated: Literal[False] = False
    sitl_world_binding_status: Literal["not_generated"] = "not_generated"
    coordinate_transform_status: Literal["not_generated"] = "not_generated"
    px4_mission_items_generated: Literal[False] = False
    route_risk_labels: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    route_plan_hash: str
    sha256: str
    planned_at: datetime

    @field_validator(
        "source_refs",
        "route_risk_labels",
        "warning_reasons",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("planned_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_route_plan(self) -> "DigitalTwinRoutePlan":
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires target ref"
            )
        if self.route_feasibility_input_source == "tile_backed_terrain":
            if not self.terrain_environment_snapshot_ref.startswith(
                "tile_backed_terrain_environment_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route plan requires tile-backed terrain ref"
                )
            if (
                self.tile_backed_terrain_environment_snapshot_ref
                != self.terrain_environment_snapshot_ref
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route plan terrain ref mismatch"
                )
            if not self.prompt_projected_terrain_environment_snapshot_ref.startswith(
                "terrain_environment_snapshot:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route plan requires prompt-projected terrain ref"
                )
            if self.route_plan_mode != "stage2_tile_backed_planning_route":
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route plan requires stage2 route plan mode"
                )
            if (
                self.source_projection_kind
                != "fixture_tile_backed_terrain_projection"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "tile-backed route plan requires tile-backed projection kind"
                )
        elif not self.terrain_environment_snapshot_ref.startswith(
            "terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires terrain snapshot ref"
            )
        if self.route_feasibility_input_source == "prompt_projected_terrain":
            if (
                self.prompt_projected_terrain_environment_snapshot_ref
                != self.terrain_environment_snapshot_ref
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "prompt-projected route plan terrain ref mismatch"
                )
            if self.tile_backed_terrain_environment_snapshot_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "prompt-projected route plan cannot bind tile-backed terrain"
                )
        if not self.weather_environment_snapshot_ref.startswith(
            "weather_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires weather snapshot ref"
            )
        if not self.digital_twin_route_feasibility_ref.startswith(
            "digital_twin_route_feasibility:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires route feasibility ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires weather policy gate ref"
            )
        required_refs = {
            self.real_world_mission_target_ref,
            self.terrain_environment_snapshot_ref,
            self.prompt_projected_terrain_environment_snapshot_ref,
            self.weather_environment_snapshot_ref,
            self.digital_twin_route_feasibility_ref,
            self.weather_environment_policy_gate_ref,
        }
        if self.tile_backed_terrain_environment_snapshot_ref:
            required_refs.add(self.tile_backed_terrain_environment_snapshot_ref)
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan requires source refs for target, terrain inputs, weather, feasibility, and gate"
            )
        if self.route_plan_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan hash mismatch"
            )
        if self.route_plan_status.startswith("blocked_") and not self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "blocked digital twin route plan requires blocked reasons"
            )
        if (
            not self.route_plan_status.startswith("blocked_")
            and self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "non-blocked digital twin route plan cannot include blocked reasons"
            )
        if self.route_plan_status == "warning_for_planning" and not (
            self.warning_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "warning digital twin route plan requires warning reasons"
            )
        if self.route_plan_status == "ready_for_planning" and (
            self.warning_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "ready digital twin route plan cannot include warning reasons"
            )
        if (
            self.route_plan_status == "blocked_by_weather_policy_gate"
            and self.weather_policy_gate_status != "blocked_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather-blocked route plan requires blocked weather policy gate"
            )
        if (
            self.route_plan_status == "blocked_by_route_feasibility"
            and self.route_feasibility_status != "blocked_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "route-feasibility-blocked route plan requires blocked route feasibility"
            )
        if self.route_plan_status == "ready_for_planning" and (
            self.route_feasibility_status != "feasible_for_planning"
            or self.weather_policy_gate_status != "passed_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "ready route plan requires passed feasibility and weather gate"
            )
        if self.route_plan_status == "warning_for_planning" and not (
            self.route_feasibility_status == "feasible_with_warnings"
            or self.weather_policy_gate_status == "warning_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "warning route plan requires warning feasibility or weather gate"
            )
        if self.route_plan_status.startswith("blocked_") and not (
            self.operator_escalation_required
        ):
            raise DigitalTwinMissionEnvironmentError(
                "blocked digital twin route plan requires operator escalation"
            )
        return self


class DigitalTwinPx4MissionItemCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION] = (
        DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION
    )
    candidate_id: str
    digital_twin_mission_anchor_candidate_ref: str = ""
    coordinate_transform_candidate_ref: str
    gazebo_world_artifact_ref: str
    gazebo_world_candidate_ref: str
    terrain_heightmap_file_artifact_ref: str
    digital_twin_route_plan_ref: str
    weather_environment_policy_gate_ref: str
    real_world_geocode_candidate_ref: str
    vehicle_flight_envelope_ref: str = ""
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_px4_mission_item_candidate"] = (
        "digital_twin_fixture_px4_mission_item_candidate"
    )
    source_url: str
    candidate_status: Literal[
        "blocked_by_weather_policy_gate",
        "blocked_by_missing_takeoff_anchor",
        "blocked_by_altitude_over_envelope",
        "candidate_generated_for_planning_only",
    ]
    candidate_items: tuple[dict[str, Any], ...] = ()
    candidate_item_count: int = Field(ge=0)
    takeoff_anchor_ref: str
    takeoff_anchor_latitude_deg: float | None = Field(default=None, ge=-90, le=90)
    takeoff_anchor_longitude_deg: float | None = Field(default=None, ge=-180, le=180)
    takeoff_anchor_altitude_m_agl: float | None = Field(default=None, ge=0)
    dropoff_target_ref: str
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    weather_policy_gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    coordinate_transform_materialized: Literal[False] = False
    takeoff_terrain_elevation_m: float | None = None
    takeoff_agl_margin_m: float | None = None
    terrain_sampling_mode: Literal[
        "anchor_point_sampled",
        "bbox_min_fallback",
    ] = "bbox_min_fallback"
    vehicle_max_takeoff_altitude_m: float | None = None
    execution_binding_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    mission_item_candidate_hash: str
    sha256: str
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...] = ()
    generated_at: datetime

    @field_validator(
        "source_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("candidate_items", mode="before")
    @classmethod
    def _coerce_candidate_items(cls, value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_candidate(self) -> "DigitalTwinPx4MissionItemCandidate":
        if (
            self.digital_twin_mission_anchor_candidate_ref
            and not self.digital_twin_mission_anchor_candidate_ref.startswith(
                "digital_twin_mission_anchor_candidate:"
            )
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires Digital Twin mission anchor candidate ref"
            )
        if not self.coordinate_transform_candidate_ref.startswith(
            "coordinate_transform_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires coordinate transform candidate ref"
            )
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires world artifact ref"
            )
        if not self.gazebo_world_candidate_ref.startswith("gazebo_world_candidate:"):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires world candidate ref"
            )
        if not self.terrain_heightmap_file_artifact_ref.startswith(
            "terrain_heightmap_file_artifact:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires heightmap file artifact ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires route plan ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires weather policy gate ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires geocode candidate ref"
            )
        required_refs = {
            self.coordinate_transform_candidate_ref,
            self.gazebo_world_artifact_ref,
            self.gazebo_world_candidate_ref,
            self.terrain_heightmap_file_artifact_ref,
            self.digital_twin_route_plan_ref,
            self.weather_environment_policy_gate_ref,
            self.real_world_geocode_candidate_ref,
        }
        if self.vehicle_flight_envelope_ref:
            if not self.vehicle_flight_envelope_ref.startswith(
                "vehicle_flight_envelope:"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "PX4 mission item candidate requires vehicle envelope ref"
                )
            required_refs.add(self.vehicle_flight_envelope_ref)
        if self.digital_twin_mission_anchor_candidate_ref:
            required_refs.add(self.digital_twin_mission_anchor_candidate_ref)
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate requires anchor, transform, world, heightmap, route plan, weather gate, and geocode source refs"
            )
        if self.source_url != STAGE2_PX4_MISSION_ITEM_CANDIDATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 PX4 mission item candidate must remain fixture sourced"
            )
        if self.candidate_item_count != len(self.candidate_items):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate count must match candidate items"
            )
        if self.dropoff_target_ref != self.real_world_geocode_candidate_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate dropoff target must remain geocode target"
            )
        if (
            self.takeoff_anchor_ref
            and not (
                self.takeoff_anchor_ref.startswith("digital_twin_fixture_anchor:")
                or self.takeoff_anchor_ref.startswith("operator_coordinate_pair:")
            )
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate takeoff anchor must be a Digital Twin fixture anchor"
            )
        if (
            self.candidate_status == "blocked_by_weather_policy_gate"
            and self.weather_policy_gate_status != "blocked_for_planning"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather-blocked PX4 mission item candidate requires blocked weather gate"
            )
        if (
            self.candidate_status == "blocked_by_weather_policy_gate"
            and "weather_policy_gate_blocked" not in set(self.blocked_reasons)
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather-blocked PX4 mission item candidate requires weather blocked reason"
            )
        if (
            self.candidate_status == "blocked_by_altitude_over_envelope"
            and "altitude_over_vehicle_envelope" not in set(self.blocked_reasons)
        ):
            raise DigitalTwinMissionEnvironmentError(
                "altitude-blocked PX4 mission item candidate requires altitude blocked reason"
            )
        if not self.takeoff_anchor_ref and "takeoff_anchor_missing" not in set(
            self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate missing takeoff anchor requires blocked reason"
            )
        if self.candidate_status.startswith("blocked_"):
            if self.candidate_items or self.candidate_item_count:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked PX4 mission item candidate cannot include mission items"
                )
            if not self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked PX4 mission item candidate requires blocked reasons"
                )
        else:
            if not self.candidate_items:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate requires candidate items"
                )
            if self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate cannot include blocked reasons"
                )
            if not self.takeoff_anchor_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate requires takeoff anchor"
                )
            if not self.digital_twin_mission_anchor_candidate_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate requires anchor candidate ref"
                )
            if (
                self.takeoff_anchor_latitude_deg is None
                or self.takeoff_anchor_longitude_deg is None
                or self.takeoff_anchor_altitude_m_agl is None
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate requires explicit takeoff anchor coordinates"
                )
            if len(self.candidate_items) != 3:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate requires takeoff, waypoint, and land items"
                )
            expected_items = (
                {
                    "seq": 0,
                    "command": "NAV_TAKEOFF",
                    "anchor_ref": self.takeoff_anchor_ref,
                    "coordinate_frame": "wgs84",
                    "latitude_deg": self.takeoff_anchor_latitude_deg,
                    "longitude_deg": self.takeoff_anchor_longitude_deg,
                    "altitude_m": self.candidate_items[0].get("altitude_m"),
                    "frame": "gazebo_world_local",
                    "candidate_only": True,
                },
                {
                    "seq": 1,
                    "command": "NAV_WAYPOINT",
                    "target_ref": self.dropoff_target_ref,
                    "coordinate_frame": "wgs84",
                    "latitude_deg": self.candidate_items[1].get("latitude_deg"),
                    "longitude_deg": self.candidate_items[1].get("longitude_deg"),
                    "altitude_m": self.candidate_items[1].get("altitude_m"),
                    "frame": "gazebo_world_local",
                    "candidate_only": True,
                },
                {
                    "seq": 2,
                    "command": "NAV_LAND",
                    "target_ref": self.dropoff_target_ref,
                    "coordinate_frame": "wgs84",
                    "latitude_deg": self.candidate_items[2].get("latitude_deg"),
                    "longitude_deg": self.candidate_items[2].get("longitude_deg"),
                    "altitude_m": self.candidate_items[2].get("altitude_m"),
                    "frame": "gazebo_world_local",
                    "candidate_only": True,
                },
            )
            if self.candidate_items != expected_items:
                raise DigitalTwinMissionEnvironmentError(
                    "generated PX4 mission item candidate items must remain planning-only and source-bound"
                )
            for item in self.candidate_items:
                if item.get("coordinate_frame") != "wgs84":
                    raise DigitalTwinMissionEnvironmentError(
                        "generated PX4 mission item candidate coordinates must be WGS84"
                    )
                latitude = item.get("latitude_deg")
                longitude = item.get("longitude_deg")
                altitude = item.get("altitude_m")
                if (
                    not isinstance(latitude, int | float)
                    or not isinstance(longitude, int | float)
                    or not isinstance(altitude, int | float)
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "generated PX4 mission item candidate requires numeric coordinates"
                    )
                if not (-90 <= float(latitude) <= 90) or not (
                    -180 <= float(longitude) <= 180
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "generated PX4 mission item candidate coordinates out of range"
                    )
                if item.get("command") == "NAV_LAND":
                    if float(altitude) < 0:
                        raise DigitalTwinMissionEnvironmentError(
                            "generated PX4 mission item candidate land altitude must be non-negative"
                        )
                elif float(altitude) <= 0:
                    raise DigitalTwinMissionEnvironmentError(
                        "generated PX4 mission item candidate altitude must be positive"
                    )
                if (
                    self.vehicle_max_takeoff_altitude_m is not None
                    and float(altitude) > self.vehicle_max_takeoff_altitude_m
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "generated PX4 mission item candidate altitude exceeds vehicle envelope"
                    )
        if self.mission_item_candidate_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate hash mismatch"
            )
        return self


class DigitalTwinMissionAnchorCandidate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION] = (
        DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION
    )
    anchor_candidate_id: str
    gazebo_world_artifact_ref: str
    gazebo_world_candidate_ref: str
    coordinate_transform_candidate_ref: str
    digital_twin_route_plan_ref: str
    real_world_geocode_candidate_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_mission_anchor_candidate"] = (
        "digital_twin_fixture_mission_anchor_candidate"
    )
    source_url: str
    anchor_candidate_status: Literal[
        "anchors_available_for_planning",
        "blocked_by_weather_policy_gate",
        "blocked_by_route_plan",
    ]
    anchor_mode: Literal[
        "fixture_or_manual_digital_twin_anchor",
        "operator_coordinate_pair_anchor",
    ] = "fixture_or_manual_digital_twin_anchor"
    takeoff_anchor_ref: str
    takeoff_anchor_latitude_deg: float | None = Field(default=None, ge=-90, le=90)
    takeoff_anchor_longitude_deg: float | None = Field(default=None, ge=-180, le=180)
    takeoff_anchor_altitude_m_agl: float | None = Field(default=None, ge=0)
    dropoff_anchor_ref: str
    dropoff_anchor_latitude_deg: float | None = Field(default=None, ge=-90, le=90)
    dropoff_anchor_longitude_deg: float | None = Field(default=None, ge=-180, le=180)
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    coordinate_transform_materialized: Literal[False] = False
    execution_binding_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    anchor_hash: str
    sha256: str
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...] = ()
    generated_at: datetime

    @field_validator(
        "source_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_anchor_candidate(self) -> "DigitalTwinMissionAnchorCandidate":
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires world artifact ref"
            )
        if not self.gazebo_world_candidate_ref.startswith("gazebo_world_candidate:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires world candidate ref"
            )
        if not self.coordinate_transform_candidate_ref.startswith(
            "coordinate_transform_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires coordinate transform candidate ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires route plan ref"
            )
        if not self.real_world_geocode_candidate_ref.startswith(
            "real_world_geocode_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires geocode candidate ref"
            )
        required_refs = {
            self.gazebo_world_artifact_ref,
            self.gazebo_world_candidate_ref,
            self.coordinate_transform_candidate_ref,
            self.digital_twin_route_plan_ref,
            self.real_world_geocode_candidate_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate requires world, transform, route plan, and geocode source refs"
            )
        if self.source_url != STAGE2_MISSION_ANCHOR_CANDIDATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 Digital Twin anchor candidate must remain fixture sourced"
            )
        if self.dropoff_anchor_ref != self.real_world_geocode_candidate_ref:
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate dropoff anchor must remain geocode target"
            )
        if self.anchor_candidate_status == "anchors_available_for_planning":
            if not self.takeoff_anchor_ref or not self.dropoff_anchor_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "available Digital Twin anchor candidate requires takeoff and dropoff anchors"
                )
            if self.anchor_mode == "fixture_or_manual_digital_twin_anchor" and (
                not self.takeoff_anchor_ref.startswith("digital_twin_fixture_anchor:")
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "available Digital Twin anchor candidate requires Digital Twin fixture takeoff anchor"
                )
            if self.anchor_mode == "operator_coordinate_pair_anchor" and (
                not self.takeoff_anchor_ref.startswith("operator_coordinate_pair:")
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "coordinate-pair anchor candidate requires operator coordinate takeoff anchor"
                )
            if (
                self.takeoff_anchor_latitude_deg is None
                or self.takeoff_anchor_longitude_deg is None
                or self.takeoff_anchor_altitude_m_agl is None
                or self.dropoff_anchor_latitude_deg is None
                or self.dropoff_anchor_longitude_deg is None
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "available Digital Twin anchor candidate requires explicit anchor coordinates"
                )
            if (
                _haversine_distance_m(
                    latitude_a=self.takeoff_anchor_latitude_deg,
                    longitude_a=self.takeoff_anchor_longitude_deg,
                    latitude_b=self.dropoff_anchor_latitude_deg,
                    longitude_b=self.dropoff_anchor_longitude_deg,
                )
                < 100.0
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "takeoff anchor collapsed onto dropoff target"
                )
            if self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "available Digital Twin anchor candidate cannot include blocked reasons"
                )
        else:
            if self.takeoff_anchor_ref:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin anchor candidate cannot include takeoff anchor"
                )
            if (
                self.takeoff_anchor_latitude_deg is not None
                or self.takeoff_anchor_longitude_deg is not None
                or self.takeoff_anchor_altitude_m_agl is not None
                or self.dropoff_anchor_latitude_deg is not None
                or self.dropoff_anchor_longitude_deg is not None
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin anchor candidate cannot include anchor coordinates"
                )
            if not self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin anchor candidate requires blocked reasons"
                )
        if self.anchor_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin anchor candidate hash mismatch"
            )
        if (
            self.anchor_candidate_status == "blocked_by_weather_policy_gate"
            and self.route_plan_status != "blocked_by_weather_policy_gate"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "weather-blocked Digital Twin anchor candidate requires weather-blocked route plan"
            )
        return self


class DigitalTwinSITLBindingGate(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_BINDING_GATE_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_BINDING_GATE_SCHEMA_VERSION
    )
    gate_id: str
    gazebo_world_artifact_ref: str
    coordinate_transform_candidate_ref: str
    digital_twin_px4_mission_item_candidate_ref: str
    weather_environment_policy_gate_ref: str
    digital_twin_route_plan_ref: str
    source_refs: tuple[str, ...]
    provider: Literal["digital_twin_fixture_sitl_binding_gate"] = (
        "digital_twin_fixture_sitl_binding_gate"
    )
    source_url: str
    binding_gate_status: Literal[
        "blocked",
        "eligible_for_operator_approved_sitl_binding",
    ]
    binding_mode: Literal["operator_approved_simulation_only"] = (
        "operator_approved_simulation_only"
    )
    binding_allowed: bool
    binding_eligible: bool
    operator_approval_required: Literal[True] = True
    server_opt_in_required: Literal[True] = True
    simulation_only: Literal[True] = True
    observed_facts_only: Literal[True] = True
    route_plan_status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    weather_policy_gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    px4_mission_item_candidate_status: Literal[
        "blocked_by_weather_policy_gate",
        "blocked_by_missing_takeoff_anchor",
        "blocked_by_altitude_over_envelope",
        "candidate_generated_for_planning_only",
    ]
    candidate_item_count: int = Field(ge=0)
    coordinate_transform_materialized: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    sitl_execution_bound: Literal[False] = False
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...] = ()
    binding_gate_hash: str
    sha256: str
    evaluated_at: datetime

    @field_validator(
        "source_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_binding_gate(self) -> "DigitalTwinSITLBindingGate":
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires world artifact ref"
            )
        if not self.coordinate_transform_candidate_ref.startswith(
            "coordinate_transform_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires coordinate transform candidate ref"
            )
        if not self.digital_twin_px4_mission_item_candidate_ref.startswith(
            "digital_twin_px4_mission_item_candidate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires PX4 mission item candidate ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires weather gate ref"
            )
        if not self.digital_twin_route_plan_ref.startswith("digital_twin_route_plan:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires route plan ref"
            )
        required_refs = {
            self.gazebo_world_artifact_ref,
            self.coordinate_transform_candidate_ref,
            self.digital_twin_px4_mission_item_candidate_ref,
            self.weather_environment_policy_gate_ref,
            self.digital_twin_route_plan_ref,
        }
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate requires world, transform, mission item, weather, and route source refs"
            )
        if self.source_url != STAGE2_SITL_BINDING_GATE_SOURCE_URL:
            raise DigitalTwinMissionEnvironmentError(
                "stage 2 Digital Twin SITL binding gate must remain fixture sourced"
            )
        if self.binding_gate_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate hash mismatch"
            )
        if self.binding_allowed:
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL binding gate cannot allow binding before operator approval and server opt-in"
            )
        if self.binding_gate_status == "blocked":
            if self.binding_allowed:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin SITL binding gate cannot allow binding"
                )
            if self.binding_eligible:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin SITL binding gate cannot be binding eligible"
                )
            if not self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "blocked Digital Twin SITL binding gate requires blocked reasons"
                )
        else:
            if not self.binding_eligible:
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate must report binding eligibility"
                )
            if self.blocked_reasons:
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate cannot include blocked reasons"
                )
            if (
                self.px4_mission_item_candidate_status
                != "candidate_generated_for_planning_only"
            ):
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate requires generated mission item candidate"
                )
            if self.candidate_item_count <= 0:
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate requires candidate items"
                )
            if self.route_plan_status.startswith("blocked_"):
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate cannot use blocked route plan"
                )
            if self.weather_policy_gate_status == "blocked_for_planning":
                raise DigitalTwinMissionEnvironmentError(
                    "eligible Digital Twin SITL binding gate cannot use blocked weather gate"
                )
        return self


class DigitalTwinStage1EpicExitResult(_DigitalTwinPlanningBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION] = (
        DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION
    )
    result_id: str
    prompt: str
    real_world_mission_target_ref: str
    terrain_environment_snapshot_ref: str
    weather_environment_snapshot_ref: str
    digital_twin_route_feasibility_ref: str
    weather_environment_policy_gate_ref: str
    digital_twin_route_plan_ref: str
    source_refs: tuple[str, ...]
    stage1_epic_exit_complete: Literal[True] = True
    prompt_constraints_observed: Literal[True] = True
    target_snapshot_observed: Literal[True] = True
    terrain_snapshot_observed: Literal[True] = True
    weather_snapshot_observed: Literal[True] = True
    route_feasibility_observed: Literal[True] = True
    weather_policy_gate_observed: Literal[True] = True
    route_plan_observed: Literal[True] = True
    requested_distance_km: float = Field(gt=0)
    requested_altitude_m: float = Field(gt=0)
    payload_weight_kg: float = Field(gt=0)
    rain_or_precipitation: Literal[True] = True
    route_feasibility_status: str
    weather_policy_gate_status: Literal["blocked_for_planning"] = (
        "blocked_for_planning"
    )
    route_plan_status: Literal["blocked_by_weather_policy_gate"] = (
        "blocked_by_weather_policy_gate"
    )
    operator_escalation_required: Literal[True] = True
    external_weather_required: Literal[True] = True
    external_weather_observed: Literal[False] = False
    digital_twin_world_generated: Literal[False] = False
    sitl_world_binding_status: Literal["not_generated"] = "not_generated"
    coordinate_transform_status: Literal["not_generated"] = "not_generated"
    px4_mission_items_generated: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    blocked_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    epic_exit_hash: str
    sha256: str
    completed_at: datetime

    @field_validator("source_refs", "blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_epic_exit(self) -> "DigitalTwinStage1EpicExitResult":
        required_refs = {
            self.real_world_mission_target_ref,
            self.terrain_environment_snapshot_ref,
            self.weather_environment_snapshot_ref,
            self.digital_twin_route_feasibility_ref,
            self.weather_environment_policy_gate_ref,
            self.digital_twin_route_plan_ref,
        }
        if not self.real_world_mission_target_ref.startswith(
            "real_world_mission_target:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires target ref"
            )
        if not self.terrain_environment_snapshot_ref.startswith(
            "terrain_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires terrain snapshot ref"
            )
        if not self.weather_environment_snapshot_ref.startswith(
            "weather_environment_snapshot:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires weather snapshot ref"
            )
        if not self.digital_twin_route_feasibility_ref.startswith(
            "digital_twin_route_feasibility:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires route feasibility ref"
            )
        if not self.weather_environment_policy_gate_ref.startswith(
            "weather_environment_policy_gate:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires weather policy gate ref"
            )
        if not self.digital_twin_route_plan_ref.startswith(
            "digital_twin_route_plan:"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires route plan ref"
            )
        if not required_refs.issubset(set(self.source_refs)):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires all stage1 source refs"
            )
        if self.epic_exit_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError("stage1 epic exit hash mismatch")
        if "weather_policy_gate_blocked" not in self.blocked_reasons:
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires weather policy blocked reason"
            )
        if (
            "external_weather_snapshot_required_for_precipitation"
            not in self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "stage1 epic exit requires missing external weather blocked reason"
            )
        return self


def real_world_mission_target_ref(target: RealWorldMissionTarget) -> str:
    return f"real_world_mission_target:{target.target_id}"


def real_world_geocode_candidate_ref(candidate: RealWorldGeocodeCandidate) -> str:
    return f"real_world_geocode_candidate:{candidate.candidate_id}"


def real_world_target_resolution_ref(resolution: RealWorldTargetResolution) -> str:
    return f"real_world_target_resolution:{resolution.resolution_id}"


def terrain_dem_tile_request_candidate_ref(
    request: TerrainDemTileRequestCandidate,
) -> str:
    return f"terrain_dem_tile_request_candidate:{request.request_id}"


def terrain_dem_source_snapshot_ref(snapshot: TerrainDemSourceSnapshot) -> str:
    return f"terrain_dem_source_snapshot:{snapshot.snapshot_id}"


def terrain_dem_tile_snapshot_ref(snapshot: TerrainDemTileSnapshot) -> str:
    return f"terrain_dem_tile_snapshot:{snapshot.snapshot_id}"


def terrain_environment_snapshot_ref(snapshot: TerrainEnvironmentSnapshot) -> str:
    return f"terrain_environment_snapshot:{snapshot.snapshot_id}"


def tile_backed_terrain_environment_snapshot_ref(
    snapshot: TileBackedTerrainEnvironmentSnapshot,
) -> str:
    return f"tile_backed_terrain_environment_snapshot:{snapshot.snapshot_id}"


def terrain_heightmap_candidate_ref(candidate: TerrainHeightmapCandidate) -> str:
    return f"terrain_heightmap_candidate:{candidate.candidate_id}"


def terrain_heightmap_artifact_ref(artifact: TerrainHeightmapArtifact) -> str:
    return f"terrain_heightmap_artifact:{artifact.artifact_id}"


def terrain_heightmap_file_artifact_ref(
    file_artifact: TerrainHeightmapFileArtifact,
) -> str:
    return f"terrain_heightmap_file_artifact:{file_artifact.file_artifact_id}"


def gazebo_world_candidate_ref(candidate: GazeboWorldCandidate) -> str:
    return f"gazebo_world_candidate:{candidate.world_candidate_id}"


def gazebo_world_artifact_ref(artifact: GazeboWorldArtifact) -> str:
    return f"gazebo_world_artifact:{artifact.world_artifact_id}"


def coordinate_transform_candidate_ref(
    candidate: CoordinateTransformCandidate,
) -> str:
    return f"coordinate_transform_candidate:{candidate.transform_candidate_id}"


def digital_twin_mission_anchor_candidate_ref(
    candidate: DigitalTwinMissionAnchorCandidate,
) -> str:
    return f"digital_twin_mission_anchor_candidate:{candidate.anchor_candidate_id}"


def digital_twin_px4_mission_item_candidate_ref(
    candidate: DigitalTwinPx4MissionItemCandidate,
) -> str:
    return f"digital_twin_px4_mission_item_candidate:{candidate.candidate_id}"


def digital_twin_sitl_binding_gate_ref(
    gate: DigitalTwinSITLBindingGate,
) -> str:
    return f"digital_twin_sitl_binding_gate:{gate.gate_id}"


def _terrain_input_ref(
    snapshot: TerrainEnvironmentSnapshot | TileBackedTerrainEnvironmentSnapshot,
) -> str:
    if isinstance(snapshot, TileBackedTerrainEnvironmentSnapshot):
        return tile_backed_terrain_environment_snapshot_ref(snapshot)
    return terrain_environment_snapshot_ref(snapshot)


def weather_environment_snapshot_ref(snapshot: WeatherEnvironmentSnapshot) -> str:
    return f"weather_environment_snapshot:{snapshot.snapshot_id}"


def weather_source_snapshot_ref(snapshot: WeatherSourceSnapshot) -> str:
    return f"weather_source_snapshot:{snapshot.snapshot_id}"


def vehicle_flight_envelope_ref(envelope: VehicleFlightEnvelope) -> str:
    return f"vehicle_flight_envelope:{envelope.envelope_id}"


def mission_energy_budget_ref(budget: MissionEnergyBudget) -> str:
    return f"mission_energy_budget:{budget.budget_id}"


def digital_twin_route_feasibility_ref(
    feasibility: DigitalTwinRouteFeasibility,
) -> str:
    return f"digital_twin_route_feasibility:{feasibility.feasibility_id}"


def weather_environment_policy_gate_ref(
    gate: WeatherEnvironmentPolicyGate,
) -> str:
    return f"weather_environment_policy_gate:{gate.gate_id}"


def digital_twin_route_plan_ref(plan: DigitalTwinRoutePlan) -> str:
    return f"digital_twin_route_plan:{plan.route_plan_id}"


def digital_twin_stage1_epic_exit_ref(
    result: DigitalTwinStage1EpicExitResult,
) -> str:
    return f"digital_twin_stage1_epic_exit:{result.result_id}"


def build_real_world_mission_target(
    *,
    prompt: str,
    prompt_request_ref: str,
    altitude_target_m: int | float | None,
    payload_weight_kg: int | float | None,
    now: datetime | None = None,
) -> RealWorldMissionTarget:
    retrieved_at = _utc(now)
    prompt_text = _clean_text(prompt)
    requested_distance_km = _extract_distance_km(prompt_text)
    requested_altitude_m = (
        float(altitude_target_m) if altitude_target_m is not None else None
    )
    payload_kg = float(payload_weight_kg) if payload_weight_kg is not None else None
    label = _target_label(prompt_text)
    status = _target_resolution_status(prompt_text)
    hash_payload = {
        "prompt_request_ref": prompt_request_ref,
        "prompt_target": prompt_text,
        "resolved_location_label": label,
        "target_resolution_status": status,
        "requested_distance_km": requested_distance_km,
        "requested_altitude_m": requested_altitude_m,
        "payload_weight_kg": payload_kg,
        "source_refs": (prompt_request_ref,),
    }
    digest = _content_hash(hash_payload)
    target_id_payload = {
        **hash_payload,
        "retrieved_at": retrieved_at.isoformat(),
        "sha256": digest,
    }
    return RealWorldMissionTarget(
        target_id=_stable_id("real_world_mission_target", target_id_payload),
        prompt_request_ref=prompt_request_ref,
        prompt_target=prompt_text,
        resolved_location_label=label,
        target_resolution_status=status,
        confidence=0.45 if status == "prompt_target_unresolved" else 0.2,
        source_url=STAGE1_TARGET_SOURCE_URL,
        source_refs=(prompt_request_ref,),
        retrieved_at=retrieved_at,
        altitude_m=requested_altitude_m,
        requested_distance_km=requested_distance_km,
        requested_altitude_m=requested_altitude_m,
        payload_weight_kg=payload_kg,
        sha256=digest,
    )


def build_real_world_geocode_candidate(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    target_resolution: RealWorldTargetResolution | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> RealWorldGeocodeCandidate:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    retrieved_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    resolution_obj = (
        target_resolution
        if isinstance(target_resolution, RealWorldTargetResolution)
        else (
            RealWorldTargetResolution.model_validate(target_resolution)
            if target_resolution
            else None
        )
    )

    if target_obj.target_resolution_status == "prompt_target_ambiguous":
        status: Literal[
            "resolved_fixture_candidate",
            "ambiguous_target_requires_operator_selection",
        ] = "ambiguous_target_requires_operator_selection"
        confidence = 0.35
    else:
        status = "resolved_fixture_candidate"
        confidence = 0.62

    latitude = resolution_obj.latitude if resolution_obj else 35.3606
    longitude = resolution_obj.longitude if resolution_obj else 138.7274
    altitude = (
        resolution_obj.altitude_m
        if resolution_obj and resolution_obj.altitude_m is not None
        else (target_obj.requested_altitude_m or target_obj.altitude_m or 120.0)
    )
    bbox = resolution_obj.bbox if resolution_obj else _bbox_around_wgs84(latitude, longitude)
    if target_obj.requested_distance_km:
        takeoff_latitude, takeoff_longitude = _wgs84_destination(
            origin_latitude=latitude,
            origin_longitude=longitude,
            bearing_deg=TAKEOFF_FROM_TARGET_BEARING_DEG,
            distance_m=float(target_obj.requested_distance_km) * 1000.0,
        )
        bbox = _bbox_covering_wgs84_points(
            (
                (latitude, longitude),
                (takeoff_latitude, takeoff_longitude),
            )
        )
    horizontal_accuracy_m = 5000.0
    source_query = target_obj.prompt_target
    geocode_mode = (
        "operator_confirmed_coordinate_pair"
        if resolution_obj
        else "fixture_backed_target_resolution"
    )
    provider = "operator_confirmed_wgs84" if resolution_obj else "digital_twin_fixture_geocoder"
    source_url = (
        resolution_obj.source_url if resolution_obj else STAGE2_GEOCODE_SOURCE_URL
    )
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "source_query": source_query,
        "resolved_location_label": target_obj.resolved_location_label,
        "candidate_status": status,
        "geocode_mode": geocode_mode,
        "provider": provider,
        "source_url": source_url,
        "coordinate_frame": "wgs84",
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": altitude,
        "horizontal_accuracy_m": horizontal_accuracy_m,
        "confidence": confidence,
        "bbox": bbox,
        "source_refs": (target_ref,),
    }
    digest = _content_hash(hash_payload)
    candidate_id_payload = {
        **hash_payload,
        "retrieved_at": retrieved_at.isoformat(),
        "geocode_hash": digest,
    }
    return RealWorldGeocodeCandidate(
        candidate_id=_stable_id("real_world_geocode_candidate", candidate_id_payload),
        real_world_mission_target_ref=target_ref,
        source_query=source_query,
        resolved_location_label=target_obj.resolved_location_label,
        candidate_status=status,
        geocode_mode=geocode_mode,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        source_url=source_url,
        source_refs=(target_ref,),
        retrieved_at=retrieved_at,
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude,
        horizontal_accuracy_m=horizontal_accuracy_m,
        confidence=confidence,
        bbox=bbox,
        geocode_hash=digest,
        sha256=digest,
    )


def build_real_world_target_resolution(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    latitude: float,
    longitude: float,
    altitude_m: int | float | None = None,
    bbox: Sequence[float] | None = None,
    horizontal_accuracy_m: int | float = 30.0,
    now: datetime | None = None,
) -> RealWorldTargetResolution:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    resolved_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    lat = float(latitude)
    lon = float(longitude)
    resolved_bbox = (
        tuple(float(item) for item in bbox)
        if bbox is not None
        else _bbox_around_wgs84(lat, lon)
    )
    if bbox is None and target_obj.requested_distance_km:
        takeoff_latitude, takeoff_longitude = _wgs84_destination(
            origin_latitude=lat,
            origin_longitude=lon,
            bearing_deg=TAKEOFF_FROM_TARGET_BEARING_DEG,
            distance_m=float(target_obj.requested_distance_km) * 1000.0,
        )
        resolved_bbox = _bbox_covering_wgs84_points(
            (
                (lat, lon),
                (takeoff_latitude, takeoff_longitude),
            )
        )
    altitude = (
        float(altitude_m)
        if altitude_m is not None
        else (
            float(target_obj.requested_altitude_m)
            if target_obj.requested_altitude_m is not None
            else None
        )
    )
    source_refs = (target_ref,)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "source_refs": source_refs,
        "target_resolution_status": "source_backed_target_resolved",
        "provider": "operator_confirmed_wgs84",
        "source_url": SOURCE_BACKED_TARGET_RESOLUTION_SOURCE_URL,
        "coordinate_frame": "wgs84",
        "latitude": round(lat, 7),
        "longitude": round(lon, 7),
        "altitude_m": altitude,
        "bbox": resolved_bbox,
        "horizontal_accuracy_m": float(horizontal_accuracy_m),
        "source_backed_target": True,
        "source_unavailable": False,
    }
    digest = _content_hash(hash_payload)
    resolution_id_payload = {
        **hash_payload,
        "resolved_at": resolved_at.isoformat(),
        "target_resolution_hash": digest,
    }
    return RealWorldTargetResolution(
        resolution_id=_stable_id(
            "real_world_target_resolution",
            resolution_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        source_refs=source_refs,
        target_resolution_status="source_backed_target_resolved",
        source_url=SOURCE_BACKED_TARGET_RESOLUTION_SOURCE_URL,
        latitude=round(lat, 7),
        longitude=round(lon, 7),
        altitude_m=altitude,
        bbox=resolved_bbox,  # type: ignore[arg-type]
        horizontal_accuracy_m=float(horizontal_accuracy_m),
        target_resolution_hash=digest,
        sha256=digest,
        resolved_at=resolved_at,
    )


def build_terrain_dem_source_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    target_resolution: RealWorldTargetResolution | Mapping[str, Any],
    now: datetime | None = None,
    fetcher: Any | None = None,
    zoom: int = 14,
    timeout_seconds: float = 20.0,
) -> TerrainDemSourceSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    resolution_obj = (
        target_resolution
        if isinstance(target_resolution, RealWorldTargetResolution)
        else RealWorldTargetResolution.model_validate(target_resolution)
    )
    captured_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    resolution_ref = real_world_target_resolution_ref(resolution_obj)
    if resolution_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source DEM target resolution target mismatch"
        )

    z, x, y = _web_mercator_tile_for_wgs84(
        resolution_obj.latitude,
        resolution_obj.longitude,
        zoom=zoom,
    )
    source_url = _gsi_dem_tile_url(z, x, y)
    terrain_context_bbox = _source_backed_terrain_context_bbox(resolution_obj.bbox)
    try:
        requested_tiles = _gsi_dem_tile_range_for_bbox(terrain_context_bbox, zoom=zoom)
    except DigitalTwinMissionEnvironmentError:
        terrain_context_bbox = resolution_obj.bbox
        requested_tiles = _gsi_dem_tile_range_for_bbox(terrain_context_bbox, zoom=zoom)
    source_refs = (target_ref, resolution_ref)

    provider_response_status = "not_requested"
    source_unavailable = False
    heightmap_normalized_heights: tuple[float, ...] = ()
    heightmap_samples_sha256 = ""
    try:
        tile_grids: dict[tuple[int, int, int], dict[str, Any]] = {}
        provider_statuses: list[str] = []
        for tile in requested_tiles:
            tile_z, tile_x, tile_y = tile
            tile_url = _gsi_dem_tile_url(tile_z, tile_x, tile_y)
            _validate_gsi_dem_source_url(tile_url)
            if fetcher is None:
                request = Request(
                    tile_url,
                    headers={"User-Agent": SOURCE_BACKED_PROVIDER_USER_AGENT},
                )
                with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                    _validate_gsi_dem_source_url(getattr(response, "url", tile_url))
                    provider_statuses.append(f"http_{getattr(response, 'status', 200)}")
                    payload_text = response.read().decode("utf-8")
            else:
                fetched = fetcher(tile_url)
                if isinstance(fetched, tuple):
                    provider_statuses.append(str(fetched[0]))
                    payload_text = str(fetched[1])
                else:
                    provider_statuses.append("injected_fetcher")
                    payload_text = str(fetched)
            (
                tile_values,
                tile_width,
                tile_height,
                _valid_values,
                _tile_no_data_ratio,
            ) = _parse_gsi_dem_grid_txt(payload_text)
            tile_grids[tile] = {
                "values": tile_values,
                "width": tile_width,
                "height": tile_height,
            }
        values, heightmap_normalized_heights, no_data_ratio = (
            _source_backed_dem_heightmap_samples(
                tile_grids=tile_grids,
                bbox=terrain_context_bbox,
                zoom=zoom,
            )
        )
        elevation_min = round(min(values), 3)
        elevation_max = round(max(values), 3)
        elevation_mean = round(sum(values) / len(values), 3)
        tile_refs = tuple(
            f"gsi-dem-tile://{tile_z}/{tile_x}/{tile_y}"
            for tile_z, tile_x, tile_y in requested_tiles
        )
        primary_status = provider_statuses[0] if provider_statuses else "not_requested"
        provider_response_status = (
            f"{primary_status};tiles={len(requested_tiles)}"
            if len(requested_tiles) != 1
            else primary_status
        )
        heightmap_samples_sha256 = sha256(
            _canonical_json_bytes({"normalized_heights": heightmap_normalized_heights})
        ).hexdigest()
        status: Literal[
            "source_backed_dem_captured",
            "blocked_source_unavailable",
        ] = "source_backed_dem_captured"
        provider: Literal["gsi_elevation_tiles", "source_backed_dem_unavailable"] = (
            "gsi_elevation_tiles"
        )
        source_backed_terrain = True
    except (DigitalTwinMissionEnvironmentError, HTTPError, URLError, TimeoutError, OSError) as exc:
        status = "blocked_source_unavailable"
        provider = "source_backed_dem_unavailable"
        if isinstance(exc, HTTPError):
            provider_response_status = f"source_unavailable:http_{exc.code}"
        else:
            provider_response_status = f"source_unavailable:{type(exc).__name__}"
        tile_refs = ()
        elevation_min = None
        elevation_max = None
        elevation_mean = None
        no_data_ratio = None
        source_unavailable = True
        source_backed_terrain = False

    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_target_resolution_ref": resolution_ref,
        "source_refs": source_refs,
        "provider": provider,
        "source_url": source_url,
        "dem_product": "gsi_dem_txt",
        "snapshot_status": status,
        "coordinate_frame": "wgs84",
        "tile_refs": tile_refs,
        "bbox": terrain_context_bbox,
        "resolution_m": 5.0,
        "elevation_min_m": elevation_min,
        "elevation_max_m": elevation_max,
        "elevation_mean_m": elevation_mean,
        "no_data_ratio": no_data_ratio,
        "heightmap_sample_source": (
            "source_dem_tile_samples" if heightmap_normalized_heights else ""
        ),
        "heightmap_sample_width": (
            SOURCE_BACKED_HEIGHTMAP_SAMPLE_WIDTH if heightmap_normalized_heights else None
        ),
        "heightmap_sample_height": (
            SOURCE_BACKED_HEIGHTMAP_SAMPLE_HEIGHT if heightmap_normalized_heights else None
        ),
        "heightmap_samples_sha256": heightmap_samples_sha256,
        "provider_response_status": provider_response_status,
        "source_backed_terrain": source_backed_terrain,
        "source_unavailable": source_unavailable,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "captured_at": captured_at.isoformat(),
        "terrain_hash": digest,
    }
    return TerrainDemSourceSnapshot(
        snapshot_id=_stable_id("terrain_dem_source_snapshot", snapshot_id_payload),
        real_world_mission_target_ref=target_ref,
        real_world_target_resolution_ref=resolution_ref,
        source_refs=source_refs,
        provider=provider,
        source_url=source_url,
        snapshot_status=status,
        tile_refs=tile_refs,
        bbox=terrain_context_bbox,
        resolution_m=5.0,
        elevation_min_m=elevation_min,
        elevation_max_m=elevation_max,
        elevation_mean_m=elevation_mean,
        no_data_ratio=no_data_ratio,
        heightmap_sample_source=(
            "source_dem_tile_samples" if heightmap_normalized_heights else ""
        ),
        heightmap_sample_width=(
            SOURCE_BACKED_HEIGHTMAP_SAMPLE_WIDTH if heightmap_normalized_heights else None
        ),
        heightmap_sample_height=(
            SOURCE_BACKED_HEIGHTMAP_SAMPLE_HEIGHT if heightmap_normalized_heights else None
        ),
        heightmap_normalized_heights=heightmap_normalized_heights,
        heightmap_samples_sha256=heightmap_samples_sha256,
        provider_response_status=provider_response_status,
        source_backed_terrain=source_backed_terrain,
        source_unavailable=source_unavailable,
        terrain_hash=digest,
        sha256=digest,
        captured_at=captured_at,
    )


def build_terrain_dem_tile_request_candidate(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainDemTileRequestCandidate:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    requested_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "DEM tile request geocode target mismatch"
        )

    if geocode_obj.candidate_status == "resolved_fixture_candidate":
        status: Literal[
            "prepared_fixture_tile_request",
            "blocked_by_ambiguous_geocode_candidate",
        ] = "prepared_fixture_tile_request"
        lat_band = int(geocode_obj.latitude * 10)
        lon_band = int(geocode_obj.longitude * 10)
        tile_refs = (
            f"dem-tile-fixture://stage2/{lat_band}/{lon_band}/primary",
            f"dem-tile-fixture://stage2/{lat_band}/{lon_band}/neighbor-east",
            f"dem-tile-fixture://stage2/{lat_band}/{lon_band}/neighbor-north",
        )
    else:
        status = "blocked_by_ambiguous_geocode_candidate"
        tile_refs = ()

    bbox = geocode_obj.bbox
    source_refs = (target_ref, geocode_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "source_refs": source_refs,
        "tile_request_status": status,
        "provider": "digital_twin_fixture_dem_tile_index",
        "source_url": STAGE2_DEM_TILE_INDEX_SOURCE_URL,
        "dem_product": "stage2_fixture_dem_30m",
        "request_mode": "fixture_backed_tile_index_lookup",
        "requested_coordinate_frame": "wgs84",
        "tile_refs": tile_refs,
        "bbox": bbox,
        "resolution_m": 30.0,
        "no_data_policy": "defer_to_dem_snapshot",
        "live_fetch_performed": False,
        "terrain_snapshot_generated": False,
        "heightmap_generated": False,
    }
    digest = _content_hash(hash_payload)
    request_id_payload = {
        **hash_payload,
        "requested_at": requested_at.isoformat(),
        "request_hash": digest,
    }
    return TerrainDemTileRequestCandidate(
        request_id=_stable_id(
            "terrain_dem_tile_request_candidate",
            request_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        source_refs=source_refs,
        tile_request_status=status,
        source_url=STAGE2_DEM_TILE_INDEX_SOURCE_URL,
        tile_refs=tile_refs,
        bbox=bbox,
        resolution_m=30.0,
        request_hash=digest,
        sha256=digest,
        requested_at=requested_at,
    )


def build_source_backed_terrain_dem_tile_request_candidate(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    target_resolution: RealWorldTargetResolution | Mapping[str, Any],
    dem_source_snapshot: TerrainDemSourceSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainDemTileRequestCandidate:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    resolution_obj = (
        target_resolution
        if isinstance(target_resolution, RealWorldTargetResolution)
        else RealWorldTargetResolution.model_validate(target_resolution)
    )
    source_obj = (
        dem_source_snapshot
        if isinstance(dem_source_snapshot, TerrainDemSourceSnapshot)
        else TerrainDemSourceSnapshot.model_validate(dem_source_snapshot)
    )
    requested_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    resolution_ref = real_world_target_resolution_ref(resolution_obj)
    source_ref = terrain_dem_source_snapshot_ref(source_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM request geocode target mismatch"
        )
    if resolution_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM request target resolution mismatch"
        )
    if source_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM request source target mismatch"
        )
    if source_obj.real_world_target_resolution_ref != resolution_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM request source resolution mismatch"
        )
    if source_obj.snapshot_status != "source_backed_dem_captured":
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM request requires captured source DEM"
        )

    source_refs = (target_ref, geocode_ref, resolution_ref, source_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "real_world_target_resolution_ref": resolution_ref,
        "source_refs": source_refs,
        "tile_request_status": "prepared_source_backed_tile_request",
        "provider": "gsi_elevation_tile_index",
        "source_url": source_obj.source_url,
        "dem_product": "gsi_dem_txt",
        "request_mode": "source_backed_gsi_tile_index_lookup",
        "requested_coordinate_frame": "wgs84",
        "tile_refs": source_obj.tile_refs,
        "bbox": source_obj.bbox,
        "resolution_m": source_obj.resolution_m,
        "no_data_policy": "defer_to_dem_snapshot",
        "live_fetch_performed": True,
        "terrain_snapshot_generated": False,
        "heightmap_generated": False,
    }
    digest = _content_hash(hash_payload)
    request_id_payload = {
        **hash_payload,
        "requested_at": requested_at.isoformat(),
        "request_hash": digest,
    }
    return TerrainDemTileRequestCandidate(
        request_id=_stable_id(
            "terrain_dem_tile_request_candidate",
            request_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        real_world_target_resolution_ref=resolution_ref,
        source_refs=source_refs,
        tile_request_status="prepared_source_backed_tile_request",
        provider="gsi_elevation_tile_index",
        source_url=source_obj.source_url,
        dem_product="gsi_dem_txt",
        request_mode="source_backed_gsi_tile_index_lookup",
        tile_refs=source_obj.tile_refs,
        bbox=source_obj.bbox,
        resolution_m=source_obj.resolution_m,
        live_fetch_performed=True,
        request_hash=digest,
        sha256=digest,
        requested_at=requested_at,
    )


def build_terrain_dem_tile_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    dem_tile_request: TerrainDemTileRequestCandidate | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainDemTileSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    request_obj = (
        dem_tile_request
        if isinstance(dem_tile_request, TerrainDemTileRequestCandidate)
        else TerrainDemTileRequestCandidate.model_validate(dem_tile_request)
    )
    captured_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    request_ref = terrain_dem_tile_request_candidate_ref(request_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "DEM tile snapshot geocode target mismatch"
        )
    if request_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "DEM tile snapshot request target mismatch"
        )
    if request_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "DEM tile snapshot request geocode mismatch"
        )
    if request_obj.tile_request_status != "prepared_fixture_tile_request":
        raise DigitalTwinMissionEnvironmentError(
            "DEM tile snapshot requires prepared tile request"
        )

    requested_altitude = float(target_obj.requested_altitude_m or 120.0)
    relief = min(max(requested_altitude * 0.06, 15.0), 300.0)
    elevation_min = max(0.0, requested_altitude - relief)
    elevation_max = requested_altitude
    elevation_mean = round((elevation_min + elevation_max) / 2.0, 3)
    source_refs = (target_ref, geocode_ref, request_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "terrain_dem_tile_request_candidate_ref": request_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_dem_tiles",
        "source_url": STAGE2_DEM_TILE_SNAPSHOT_SOURCE_URL,
        "snapshot_mode": "fixture_backed_dem_tile_snapshot",
        "coordinate_frame": "wgs84",
        "tile_refs": request_obj.tile_refs,
        "bbox": request_obj.bbox,
        "resolution_m": request_obj.resolution_m,
        "elevation_min_m": round(elevation_min, 3),
        "elevation_max_m": round(elevation_max, 3),
        "elevation_mean_m": elevation_mean,
        "no_data_ratio": 0.0,
        "live_fetch_performed": False,
        "heightmap_generated": False,
        "digital_twin_world_generated": False,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "captured_at": captured_at.isoformat(),
        "terrain_hash": digest,
    }
    return TerrainDemTileSnapshot(
        snapshot_id=_stable_id("terrain_dem_tile_snapshot", snapshot_id_payload),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        terrain_dem_tile_request_candidate_ref=request_ref,
        source_refs=source_refs,
        source_url=STAGE2_DEM_TILE_SNAPSHOT_SOURCE_URL,
        tile_refs=request_obj.tile_refs,
        bbox=request_obj.bbox,
        resolution_m=request_obj.resolution_m,
        elevation_min_m=round(elevation_min, 3),
        elevation_max_m=round(elevation_max, 3),
        elevation_mean_m=elevation_mean,
        no_data_ratio=0.0,
        terrain_hash=digest,
        sha256=digest,
        captured_at=captured_at,
    )


def build_terrain_dem_tile_snapshot_from_source_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    dem_tile_request: TerrainDemTileRequestCandidate | Mapping[str, Any],
    dem_source_snapshot: TerrainDemSourceSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainDemTileSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    request_obj = (
        dem_tile_request
        if isinstance(dem_tile_request, TerrainDemTileRequestCandidate)
        else TerrainDemTileRequestCandidate.model_validate(dem_tile_request)
    )
    source_obj = (
        dem_source_snapshot
        if isinstance(dem_source_snapshot, TerrainDemSourceSnapshot)
        else TerrainDemSourceSnapshot.model_validate(dem_source_snapshot)
    )
    captured_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    request_ref = terrain_dem_tile_request_candidate_ref(request_obj)
    source_ref = terrain_dem_source_snapshot_ref(source_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot geocode target mismatch"
        )
    if request_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot request target mismatch"
        )
    if request_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot request geocode mismatch"
        )
    if request_obj.tile_request_status != "prepared_source_backed_tile_request":
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot requires source-backed request"
        )
    if source_obj.snapshot_status != "source_backed_dem_captured":
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot requires captured source DEM"
        )
    if request_obj.tile_refs != source_obj.tile_refs:
        raise DigitalTwinMissionEnvironmentError(
            "source-backed DEM tile snapshot tile refs mismatch"
        )

    source_refs = (target_ref, geocode_ref, request_ref, source_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "terrain_dem_tile_request_candidate_ref": request_ref,
        "terrain_dem_source_snapshot_ref": source_ref,
        "source_refs": source_refs,
        "provider": "gsi_elevation_tiles",
        "source_url": source_obj.source_url,
        "snapshot_mode": "source_backed_gsi_dem_tile_snapshot",
        "coordinate_frame": "wgs84",
        "tile_refs": source_obj.tile_refs,
        "bbox": source_obj.bbox,
        "resolution_m": source_obj.resolution_m,
        "elevation_min_m": source_obj.elevation_min_m,
        "elevation_max_m": source_obj.elevation_max_m,
        "elevation_mean_m": source_obj.elevation_mean_m,
        "no_data_ratio": source_obj.no_data_ratio,
        "heightmap_sample_source": source_obj.heightmap_sample_source,
        "heightmap_sample_width": source_obj.heightmap_sample_width,
        "heightmap_sample_height": source_obj.heightmap_sample_height,
        "heightmap_samples_sha256": source_obj.heightmap_samples_sha256,
        "live_fetch_performed": True,
        "heightmap_generated": False,
        "digital_twin_world_generated": False,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "captured_at": captured_at.isoformat(),
        "terrain_hash": digest,
    }
    return TerrainDemTileSnapshot(
        snapshot_id=_stable_id("terrain_dem_tile_snapshot", snapshot_id_payload),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        terrain_dem_tile_request_candidate_ref=request_ref,
        terrain_dem_source_snapshot_ref=source_ref,
        source_refs=source_refs,
        provider="gsi_elevation_tiles",
        source_url=source_obj.source_url,
        snapshot_mode="source_backed_gsi_dem_tile_snapshot",
        tile_refs=source_obj.tile_refs,
        bbox=source_obj.bbox,
        resolution_m=source_obj.resolution_m,
        elevation_min_m=float(source_obj.elevation_min_m),
        elevation_max_m=float(source_obj.elevation_max_m),
        elevation_mean_m=float(source_obj.elevation_mean_m),
        no_data_ratio=float(source_obj.no_data_ratio),
        heightmap_sample_source=source_obj.heightmap_sample_source,
        heightmap_sample_width=source_obj.heightmap_sample_width,
        heightmap_sample_height=source_obj.heightmap_sample_height,
        heightmap_normalized_heights=source_obj.heightmap_normalized_heights,
        heightmap_samples_sha256=source_obj.heightmap_samples_sha256,
        terrain_hash=digest,
        sha256=digest,
        live_fetch_performed=True,
        captured_at=captured_at,
    )


def build_terrain_environment_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainEnvironmentSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    retrieved_at = _utc(now)
    distance_m = max((target_obj.requested_distance_km or 1.0) * 1000.0, 1.0)
    altitude = float(target_obj.requested_altitude_m or target_obj.altitude_m or 120.0)
    relief = min(max(altitude * 0.08, 20.0), 400.0)
    elevation_min = max(0.0, altitude - relief)
    elevation_max = altitude
    slope_ratio = (elevation_max - elevation_min) / distance_m
    slope_label: Literal["unknown", "low", "moderate", "high"]
    if target_obj.target_resolution_status == "prompt_target_ambiguous":
        slope_label = "unknown"
    elif slope_ratio >= 0.08:
        slope_label = "high"
    elif slope_ratio >= 0.03:
        slope_label = "moderate"
    else:
        slope_label = "low"
    bbox = (0.0, 0.0, distance_m, distance_m)
    tile_ref = (
        "dem-fixture://digital-twin-stage1/"
        f"{target_obj.resolved_location_label}/{int(round(altitude))}m"
    )
    hash_payload = {
        "real_world_mission_target_ref": real_world_mission_target_ref(target_obj),
        "provider": "digital_twin_fixture_dem",
        "source_url": STAGE1_TERRAIN_SOURCE_URL,
        "tile_refs": (tile_ref,),
        "bbox": bbox,
        "resolution_m": 30.0,
        "elevation_min_m": round(elevation_min, 3),
        "elevation_max_m": round(elevation_max, 3),
        "slope_risk_label": slope_label,
        "no_data_ratio": 0.0,
        "snapshot_mode": "prompt_projected_fixture",
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "retrieved_at": retrieved_at.isoformat(),
        "terrain_hash": digest,
    }
    return TerrainEnvironmentSnapshot(
        snapshot_id=_stable_id(
            "terrain_environment_snapshot",
            snapshot_id_payload,
        ),
        real_world_mission_target_ref=real_world_mission_target_ref(target_obj),
        source_url=STAGE1_TERRAIN_SOURCE_URL,
        source_refs=(real_world_mission_target_ref(target_obj),),
        retrieved_at=retrieved_at,
        bbox=bbox,
        tile_refs=(tile_ref,),
        resolution_m=30.0,
        elevation_min_m=round(elevation_min, 3),
        elevation_max_m=round(elevation_max, 3),
        slope_risk_label=slope_label,
        no_data_ratio=0.0,
        terrain_hash=digest,
        sha256=digest,
    )


def build_tile_backed_terrain_environment_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    dem_tile_snapshot: TerrainDemTileSnapshot | Mapping[str, Any],
    route_feasibility_binding_status: Literal[
        "not_bound",
        "bound_to_route_feasibility",
    ] = "not_bound",
    now: datetime | None = None,
) -> TileBackedTerrainEnvironmentSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    dem_obj = (
        dem_tile_snapshot
        if isinstance(dem_tile_snapshot, TerrainDemTileSnapshot)
        else TerrainDemTileSnapshot.model_validate(dem_tile_snapshot)
    )
    captured_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    dem_ref = terrain_dem_tile_snapshot_ref(dem_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "tile-backed terrain geocode target mismatch"
        )
    if dem_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "tile-backed terrain DEM target mismatch"
        )
    if dem_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "tile-backed terrain DEM geocode mismatch"
        )

    distance_m = max((target_obj.requested_distance_km or 1.0) * 1000.0, 1.0)
    elevation_gain = max(0.0, dem_obj.elevation_max_m - dem_obj.elevation_min_m)
    slope_ratio = elevation_gain / distance_m
    slope_label: Literal["unknown", "low", "moderate", "high"]
    if target_obj.target_resolution_status == "prompt_target_ambiguous":
        slope_label = "unknown"
    elif slope_ratio >= 0.08:
        slope_label = "high"
    elif slope_ratio >= 0.03:
        slope_label = "moderate"
    else:
        slope_label = "low"
    source_refs = (target_ref, geocode_ref, dem_ref)
    if dem_obj.provider == "gsi_elevation_tiles":
        provider = "gsi_elevation_tile_backed_terrain"
        source_url = dem_obj.source_url
        snapshot_mode = "source_backed_gsi_tile_terrain"
        live_fetch_performed = True
    else:
        provider = "digital_twin_fixture_tile_backed_terrain"
        source_url = STAGE2_TILE_BACKED_TERRAIN_SOURCE_URL
        snapshot_mode = "tile_backed_fixture_terrain"
        live_fetch_performed = False
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "terrain_dem_tile_snapshot_ref": dem_ref,
        "source_refs": source_refs,
        "provider": provider,
        "source_url": source_url,
        "snapshot_mode": snapshot_mode,
        "route_feasibility_binding_status": route_feasibility_binding_status,
        "coordinate_frame": "wgs84",
        "tile_refs": dem_obj.tile_refs,
        "bbox": dem_obj.bbox,
        "resolution_m": dem_obj.resolution_m,
        "elevation_min_m": dem_obj.elevation_min_m,
        "elevation_max_m": dem_obj.elevation_max_m,
        "elevation_mean_m": dem_obj.elevation_mean_m,
        "slope_risk_label": slope_label,
        "no_data_ratio": dem_obj.no_data_ratio,
        "live_fetch_performed": live_fetch_performed,
        "heightmap_generated": False,
        "digital_twin_world_generated": False,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "captured_at": captured_at.isoformat(),
        "terrain_hash": digest,
    }
    return TileBackedTerrainEnvironmentSnapshot(
        snapshot_id=_stable_id(
            "tile_backed_terrain_environment_snapshot",
            snapshot_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        terrain_dem_tile_snapshot_ref=dem_ref,
        source_refs=source_refs,
        provider=provider,
        source_url=source_url,
        snapshot_mode=snapshot_mode,
        route_feasibility_binding_status=route_feasibility_binding_status,
        tile_refs=dem_obj.tile_refs,
        bbox=dem_obj.bbox,
        resolution_m=dem_obj.resolution_m,
        elevation_min_m=dem_obj.elevation_min_m,
        elevation_max_m=dem_obj.elevation_max_m,
        elevation_mean_m=dem_obj.elevation_mean_m,
        slope_risk_label=slope_label,
        no_data_ratio=dem_obj.no_data_ratio,
        terrain_hash=digest,
        sha256=digest,
        live_fetch_performed=live_fetch_performed,
        captured_at=captured_at,
    )


def build_terrain_heightmap_candidate(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    dem_tile_snapshot: TerrainDemTileSnapshot | Mapping[str, Any],
    tile_backed_terrain: TileBackedTerrainEnvironmentSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainHeightmapCandidate:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    dem_obj = (
        dem_tile_snapshot
        if isinstance(dem_tile_snapshot, TerrainDemTileSnapshot)
        else TerrainDemTileSnapshot.model_validate(dem_tile_snapshot)
    )
    terrain_obj = (
        tile_backed_terrain
        if isinstance(tile_backed_terrain, TileBackedTerrainEnvironmentSnapshot)
        else TileBackedTerrainEnvironmentSnapshot.model_validate(tile_backed_terrain)
    )
    generated_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    dem_ref = terrain_dem_tile_snapshot_ref(dem_obj)
    terrain_ref = tile_backed_terrain_environment_snapshot_ref(terrain_obj)
    if geocode_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate geocode target mismatch"
        )
    if dem_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate DEM target mismatch"
        )
    if dem_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate DEM geocode mismatch"
        )
    if terrain_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate tile terrain target mismatch"
        )
    if terrain_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate tile terrain geocode mismatch"
        )
    if terrain_obj.terrain_dem_tile_snapshot_ref != dem_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap candidate tile terrain DEM mismatch"
        )

    width = 64
    height = 64
    horizontal_resolution_m = float(terrain_obj.resolution_m)
    if terrain_obj.provider == "gsi_elevation_tile_backed_terrain":
        requested_distance_m = max(
            (target_obj.requested_distance_km or 1.0) * 1000.0,
            1.0,
        )
        required_extent_m = requested_distance_m + SOURCE_BACKED_ROUTE_MARGIN_M
        horizontal_resolution_m = round(
            max(
                horizontal_resolution_m,
                required_extent_m / float(width - 1),
            ),
            3,
        )
    vertical_scale = round(
        terrain_obj.elevation_max_m - terrain_obj.elevation_min_m,
        3,
    )
    source_dem_samples = (
        dem_obj.heightmap_normalized_heights
        if (
            dem_obj.heightmap_sample_source == "source_dem_tile_samples"
            and dem_obj.heightmap_sample_width == width
            and dem_obj.heightmap_sample_height == height
            and len(dem_obj.heightmap_normalized_heights) == width * height
        )
        else ()
    )
    heightmap_sample_source: Literal[
        "synthetic_fixture_gradient",
        "source_dem_tile_samples",
    ] = (
        "source_dem_tile_samples" if source_dem_samples else "synthetic_fixture_gradient"
    )
    heightmap_samples_sha256 = (
        dem_obj.heightmap_samples_sha256
        if source_dem_samples
        else sha256(
            _canonical_json_bytes(
                {
                    "normalized_heights": _normalized_heightmap_samples(
                        width,
                        height,
                    )
                }
            )
        ).hexdigest()
    )
    source_refs = (target_ref, geocode_ref, dem_ref, terrain_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "terrain_dem_tile_snapshot_ref": dem_ref,
        "tile_backed_terrain_environment_snapshot_ref": terrain_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_heightmap_candidate",
        "source_url": STAGE2_HEIGHTMAP_CANDIDATE_SOURCE_URL,
        "candidate_mode": "fixture_tile_backed_heightmap_candidate",
        "heightmap_status": "candidate_generated",
        "coordinate_frame": "wgs84",
        "height_encoding": "normalized_float32_grid",
        "pixel_width": width,
        "pixel_height": height,
        "horizontal_resolution_m": horizontal_resolution_m,
        "vertical_scale_m": vertical_scale,
        "elevation_min_m": terrain_obj.elevation_min_m,
        "elevation_max_m": terrain_obj.elevation_max_m,
        "elevation_mean_m": terrain_obj.elevation_mean_m,
        "normalized_min": 0.0,
        "normalized_max": 1.0,
        "heightmap_sample_source": heightmap_sample_source,
        "heightmap_samples_sha256": heightmap_samples_sha256,
        "tile_refs": terrain_obj.tile_refs,
        "bbox": terrain_obj.bbox,
        "no_data_ratio": terrain_obj.no_data_ratio,
        "artifact_materialized": False,
        "gazebo_world_generated": False,
        "px4_mission_items_generated": False,
    }
    digest = _content_hash(hash_payload)
    candidate_id_payload = {
        **hash_payload,
        "generated_at": generated_at.isoformat(),
        "heightmap_hash": digest,
    }
    return TerrainHeightmapCandidate(
        candidate_id=_stable_id("terrain_heightmap_candidate", candidate_id_payload),
        real_world_mission_target_ref=target_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        terrain_dem_tile_snapshot_ref=dem_ref,
        tile_backed_terrain_environment_snapshot_ref=terrain_ref,
        source_refs=source_refs,
        source_url=STAGE2_HEIGHTMAP_CANDIDATE_SOURCE_URL,
        pixel_width=width,
        pixel_height=height,
        horizontal_resolution_m=horizontal_resolution_m,
        vertical_scale_m=vertical_scale,
        elevation_min_m=terrain_obj.elevation_min_m,
        elevation_max_m=terrain_obj.elevation_max_m,
        elevation_mean_m=terrain_obj.elevation_mean_m,
        tile_refs=terrain_obj.tile_refs,
        bbox=terrain_obj.bbox,
        no_data_ratio=terrain_obj.no_data_ratio,
        heightmap_sample_source=heightmap_sample_source,
        heightmap_normalized_heights=source_dem_samples,
        heightmap_samples_sha256=heightmap_samples_sha256,
        heightmap_hash=digest,
        sha256=digest,
        generated_at=generated_at,
    )


def build_terrain_heightmap_artifact(
    *,
    heightmap_candidate: TerrainHeightmapCandidate | Mapping[str, Any],
    dem_tile_snapshot: TerrainDemTileSnapshot | Mapping[str, Any],
    tile_backed_terrain: TileBackedTerrainEnvironmentSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> TerrainHeightmapArtifact:
    candidate_obj = (
        heightmap_candidate
        if isinstance(heightmap_candidate, TerrainHeightmapCandidate)
        else TerrainHeightmapCandidate.model_validate(heightmap_candidate)
    )
    dem_obj = (
        dem_tile_snapshot
        if isinstance(dem_tile_snapshot, TerrainDemTileSnapshot)
        else TerrainDemTileSnapshot.model_validate(dem_tile_snapshot)
    )
    terrain_obj = (
        tile_backed_terrain
        if isinstance(tile_backed_terrain, TileBackedTerrainEnvironmentSnapshot)
        else TileBackedTerrainEnvironmentSnapshot.model_validate(tile_backed_terrain)
    )
    generated_at = _utc(now)
    candidate_ref = terrain_heightmap_candidate_ref(candidate_obj)
    dem_ref = terrain_dem_tile_snapshot_ref(dem_obj)
    terrain_ref = tile_backed_terrain_environment_snapshot_ref(terrain_obj)
    if candidate_obj.terrain_dem_tile_snapshot_ref != dem_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact candidate DEM mismatch"
        )
    if candidate_obj.tile_backed_terrain_environment_snapshot_ref != terrain_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact candidate tile terrain mismatch"
        )
    if terrain_obj.terrain_dem_tile_snapshot_ref != dem_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact tile terrain DEM mismatch"
        )
    if candidate_obj.tile_refs != terrain_obj.tile_refs:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact tile refs mismatch"
        )
    if candidate_obj.bbox != terrain_obj.bbox:
        raise DigitalTwinMissionEnvironmentError("heightmap artifact bbox mismatch")
    if candidate_obj.elevation_min_m != terrain_obj.elevation_min_m:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact elevation min mismatch"
        )
    if candidate_obj.elevation_max_m != terrain_obj.elevation_max_m:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact elevation max mismatch"
        )
    if candidate_obj.elevation_mean_m != terrain_obj.elevation_mean_m:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap artifact elevation mean mismatch"
        )

    source_refs = _text_tuple(
        (
            candidate_ref,
            *candidate_obj.source_refs,
            dem_ref,
            terrain_ref,
        )
    )
    artifact_payload = {
        "schema_version": TERRAIN_HEIGHTMAP_ARTIFACT_SCHEMA_VERSION,
        "provider": "digital_twin_fixture_heightmap_artifact",
        "source_url": STAGE2_HEIGHTMAP_ARTIFACT_SOURCE_URL,
        "artifact_status": "materialized",
        "artifact_materialized": True,
        "artifact_format": "normalized_heightmap_grid_json",
        "encoding": "row_major_normalized_float32",
        "coordinate_frame": "wgs84",
        "pixel_width": candidate_obj.pixel_width,
        "pixel_height": candidate_obj.pixel_height,
        "horizontal_resolution_m": candidate_obj.horizontal_resolution_m,
        "vertical_scale_m": candidate_obj.vertical_scale_m,
        "elevation_min_m": candidate_obj.elevation_min_m,
        "elevation_max_m": candidate_obj.elevation_max_m,
        "elevation_mean_m": candidate_obj.elevation_mean_m,
        "normalized_min": candidate_obj.normalized_min,
        "normalized_max": candidate_obj.normalized_max,
        "bbox": candidate_obj.bbox,
        "tile_refs": candidate_obj.tile_refs,
        "no_data_ratio": candidate_obj.no_data_ratio,
        "candidate_hash": candidate_obj.heightmap_hash,
        "gazebo_world_generated": False,
        "coordinate_transform_generated": False,
        "px4_mission_items_generated": False,
    }
    digest = _content_hash(artifact_payload)
    artifact_id_payload = {
        **artifact_payload,
        "heightmap_candidate_ref": candidate_ref,
        "terrain_dem_tile_snapshot_ref": dem_ref,
        "tile_backed_terrain_environment_snapshot_ref": terrain_ref,
        "source_refs": source_refs,
        "generated_at": generated_at.isoformat(),
        "artifact_sha256": digest,
    }
    return TerrainHeightmapArtifact(
        artifact_id=_stable_id("terrain_heightmap_artifact", artifact_id_payload),
        heightmap_candidate_ref=candidate_ref,
        terrain_dem_tile_snapshot_ref=dem_ref,
        tile_backed_terrain_environment_snapshot_ref=terrain_ref,
        source_refs=source_refs,
        source_url=STAGE2_HEIGHTMAP_ARTIFACT_SOURCE_URL,
        pixel_width=candidate_obj.pixel_width,
        pixel_height=candidate_obj.pixel_height,
        horizontal_resolution_m=candidate_obj.horizontal_resolution_m,
        vertical_scale_m=candidate_obj.vertical_scale_m,
        elevation_min_m=candidate_obj.elevation_min_m,
        elevation_max_m=candidate_obj.elevation_max_m,
        elevation_mean_m=candidate_obj.elevation_mean_m,
        bbox=candidate_obj.bbox,
        tile_refs=candidate_obj.tile_refs,
        no_data_ratio=candidate_obj.no_data_ratio,
        candidate_hash=candidate_obj.heightmap_hash,
        artifact_sha256=digest,
        sha256=digest,
        generated_at=generated_at,
    )


def _normalized_heightmap_samples(width: int, height: int) -> tuple[float, ...]:
    values: list[float] = []
    for row in range(height):
        row_ratio = row / (height - 1)
        for col in range(width):
            col_ratio = col / (width - 1)
            variation = (col_ratio - 0.5) * 0.04
            values.append(round(min(1.0, max(0.0, row_ratio + variation)), 6))
    return tuple(values)


def _portable_graymap_bytes(
    *,
    width: int,
    height: int,
    normalized_heights: Sequence[float],
) -> bytes:
    expected_sample_count = width * height
    if len(normalized_heights) != expected_sample_count:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap Gazebo DEM sample count mismatch"
        )
    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    pixels = bytes(
        int(round(max(0.0, min(1.0, float(value))) * 255))
        for value in normalized_heights
    )
    return header + pixels


def _sample_heightmap_file_at_wgs84(
    file_obj: TerrainHeightmapFileArtifact,
    *,
    latitude: float | None,
    longitude: float | None,
    require_anchor_point_sample: bool,
) -> tuple[float, Literal["anchor_point_sampled", "bbox_min_fallback"]]:
    if not require_anchor_point_sample:
        return round(float(file_obj.elevation_min_m), 3), "bbox_min_fallback"
    if latitude is None or longitude is None:
        raise DigitalTwinMissionEnvironmentError(
            "anchor terrain sampling requires takeoff coordinates"
        )
    if not _wgs84_inside_bbox(
        latitude=float(latitude),
        longitude=float(longitude),
        bbox=file_obj.bbox,
    ):
        raise DigitalTwinMissionEnvironmentError(
            "anchor terrain sample coordinate outside heightmap bbox"
        )
    path = Path(file_obj.file_path_or_artifact_uri)
    if not path.exists() or path.suffix != ".json":
        raise DigitalTwinMissionEnvironmentError(
            "anchor terrain sampling requires materialized heightmap JSON"
        )
    payload_bytes = path.read_bytes()
    if sha256(payload_bytes).hexdigest() != file_obj.file_sha256:
        raise DigitalTwinMissionEnvironmentError(
            "anchor terrain sampling heightmap file hash mismatch"
        )
    payload = json.loads(payload_bytes.decode("utf-8"))
    heights = tuple(float(value) for value in payload.get("normalized_heights", ()))
    expected = file_obj.pixel_width * file_obj.pixel_height
    if len(heights) != expected:
        raise DigitalTwinMissionEnvironmentError(
            "anchor terrain sampling heightmap sample count mismatch"
        )
    lat_min, lon_min, lat_max, lon_max = (float(item) for item in file_obj.bbox)
    lat_ratio = (
        (float(latitude) - lat_min) / (lat_max - lat_min)
        if lat_max != lat_min
        else 0.0
    )
    lon_ratio = (
        (float(longitude) - lon_min) / (lon_max - lon_min)
        if lon_max != lon_min
        else 0.0
    )
    row = int(round(max(0.0, min(1.0, lat_ratio)) * (file_obj.pixel_height - 1)))
    col = int(round(max(0.0, min(1.0, lon_ratio)) * (file_obj.pixel_width - 1)))
    normalized = max(0.0, min(1.0, heights[row * file_obj.pixel_width + col]))
    elevation = file_obj.elevation_min_m + normalized * file_obj.vertical_scale_m
    return round(float(elevation), 3), "anchor_point_sampled"


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _xml_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _deterministic_gazebo_world_sdf(
    *,
    world_candidate: GazeboWorldCandidate,
) -> bytes:
    terrain_x, terrain_y, terrain_z = world_candidate.terrain_scale
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sdf version="1.9">',
        '  <world name="digital_twin_stage2_planning_only">',
        "    <gravity>0 0 -9.8</gravity>",
        "    <scene>",
        "      <ambient>0.4 0.4 0.4 1</ambient>",
        "      <background>0.7 0.8 1 1</background>",
        "    </scene>",
        (
            "    <!-- planning_only=true; execution_binding_allowed=false; "
            "coordinate_transform_generated=false; px4_mission_items_generated=false -->"
        ),
        '    <model name="digital_twin_heightmap_terrain">',
        "      <static>true</static>",
        '      <link name="terrain_link">',
        '        <collision name="terrain_collision">',
        "          <geometry>",
        "            <heightmap>",
        f"              <uri>{_xml_escape(world_candidate.heightmap_uri)}</uri>",
        f"              <size>{terrain_x:.3f} {terrain_y:.3f} {terrain_z:.3f}</size>",
        "              <pos>0 0 0</pos>",
        "            </heightmap>",
        "          </geometry>",
        "        </collision>",
        '        <visual name="terrain_visual">',
        "          <geometry>",
        "            <heightmap>",
        f"              <uri>{_xml_escape(world_candidate.heightmap_uri)}</uri>",
        f"              <size>{terrain_x:.3f} {terrain_y:.3f} {terrain_z:.3f}</size>",
        "              <pos>0 0 0</pos>",
        "            </heightmap>",
        "          </geometry>",
        "        </visual>",
        "      </link>",
        "    </model>",
        "  </world>",
        "</sdf>",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_terrain_heightmap_file_artifact(
    *,
    heightmap_artifact: TerrainHeightmapArtifact | Mapping[str, Any],
    heightmap_candidate: TerrainHeightmapCandidate | Mapping[str, Any],
    now: datetime | None = None,
    file_root: Path | str | None = None,
) -> TerrainHeightmapFileArtifact:
    artifact_obj = (
        heightmap_artifact
        if isinstance(heightmap_artifact, TerrainHeightmapArtifact)
        else TerrainHeightmapArtifact.model_validate(heightmap_artifact)
    )
    candidate_obj = (
        heightmap_candidate
        if isinstance(heightmap_candidate, TerrainHeightmapCandidate)
        else TerrainHeightmapCandidate.model_validate(heightmap_candidate)
    )
    generated_at = _utc(now)
    artifact_ref = terrain_heightmap_artifact_ref(artifact_obj)
    candidate_ref = terrain_heightmap_candidate_ref(candidate_obj)
    if artifact_obj.heightmap_candidate_ref != candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact candidate ref mismatch"
        )
    if artifact_obj.candidate_hash != candidate_obj.heightmap_hash:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact candidate hash mismatch"
        )
    if artifact_obj.pixel_width != candidate_obj.pixel_width:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact pixel width mismatch"
        )
    if artifact_obj.pixel_height != candidate_obj.pixel_height:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact pixel height mismatch"
        )
    if artifact_obj.vertical_scale_m != candidate_obj.vertical_scale_m:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact vertical scale mismatch"
        )
    if artifact_obj.bbox != candidate_obj.bbox:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact bbox mismatch"
        )
    if artifact_obj.tile_refs != candidate_obj.tile_refs:
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact tile refs mismatch"
        )

    source_refs = _text_tuple((artifact_ref, *artifact_obj.source_refs))
    normalized_heights = (
        candidate_obj.heightmap_normalized_heights
        if len(candidate_obj.heightmap_normalized_heights)
        == artifact_obj.pixel_width * artifact_obj.pixel_height
        else _normalized_heightmap_samples(
            artifact_obj.pixel_width,
            artifact_obj.pixel_height,
        )
    )
    normalized_heights_sha256 = sha256(
        _canonical_json_bytes({"normalized_heights": normalized_heights})
    ).hexdigest()
    if (
        candidate_obj.heightmap_normalized_heights
        and candidate_obj.heightmap_samples_sha256 != normalized_heights_sha256
    ):
        raise DigitalTwinMissionEnvironmentError(
            "heightmap file artifact candidate sample hash mismatch"
        )
    file_payload = {
        "schema_version": HEIGHTMAP_FILE_PAYLOAD_SCHEMA_VERSION,
        "artifact_sha256": artifact_obj.artifact_sha256,
        "candidate_hash": candidate_obj.heightmap_hash,
        "heightmap_sample_source": candidate_obj.heightmap_sample_source,
        "heightmap_samples_sha256": normalized_heights_sha256,
        "file_format": "normalized_heightmap_grid_json",
        "encoding": "row_major_normalized_float32",
        "gazebo_dem_file_format": "portable_graymap_p5",
        "gazebo_dem_encoding": "uint8_grayscale_heightmap",
        "coordinate_frame": "wgs84",
        "pixel_width": artifact_obj.pixel_width,
        "pixel_height": artifact_obj.pixel_height,
        "horizontal_resolution_m": artifact_obj.horizontal_resolution_m,
        "vertical_scale_m": artifact_obj.vertical_scale_m,
        "elevation_min_m": artifact_obj.elevation_min_m,
        "elevation_max_m": artifact_obj.elevation_max_m,
        "elevation_mean_m": artifact_obj.elevation_mean_m,
        "normalized_min": artifact_obj.normalized_min,
        "normalized_max": artifact_obj.normalized_max,
        "bbox": artifact_obj.bbox,
        "tile_refs": artifact_obj.tile_refs,
        "no_data_ratio": artifact_obj.no_data_ratio,
        "normalized_heights": normalized_heights,
    }
    file_bytes = _canonical_json_bytes(file_payload)
    file_digest = sha256(file_bytes).hexdigest()
    gazebo_dem_bytes = _portable_graymap_bytes(
        width=artifact_obj.pixel_width,
        height=artifact_obj.pixel_height,
        normalized_heights=normalized_heights,
    )
    gazebo_dem_digest = sha256(gazebo_dem_bytes).hexdigest()
    root = Path(file_root) if file_root is not None else HEIGHTMAP_FILE_ARTIFACT_ROOT
    file_path = root / f"{file_digest}.heightmap.json"
    gazebo_dem_path = root / f"{gazebo_dem_digest}.heightmap.pgm"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists() or file_path.read_bytes() != file_bytes:
        file_path.write_bytes(file_bytes)
    if (
        not gazebo_dem_path.exists()
        or gazebo_dem_path.read_bytes() != gazebo_dem_bytes
    ):
        gazebo_dem_path.write_bytes(gazebo_dem_bytes)

    return TerrainHeightmapFileArtifact(
        file_artifact_id=_stable_id(
            "terrain_heightmap_file_artifact",
            {
                "schema_version": TERRAIN_HEIGHTMAP_FILE_ARTIFACT_SCHEMA_VERSION,
                "file_sha256": file_digest,
            },
        ),
        terrain_heightmap_artifact_ref=artifact_ref,
        terrain_heightmap_candidate_ref=candidate_ref,
        terrain_dem_tile_snapshot_ref=artifact_obj.terrain_dem_tile_snapshot_ref,
        tile_backed_terrain_environment_snapshot_ref=(
            artifact_obj.tile_backed_terrain_environment_snapshot_ref
        ),
        source_refs=source_refs,
        source_url=STAGE2_HEIGHTMAP_FILE_ARTIFACT_SOURCE_URL,
        pixel_width=artifact_obj.pixel_width,
        pixel_height=artifact_obj.pixel_height,
        horizontal_resolution_m=artifact_obj.horizontal_resolution_m,
        vertical_scale_m=artifact_obj.vertical_scale_m,
        elevation_min_m=artifact_obj.elevation_min_m,
        elevation_max_m=artifact_obj.elevation_max_m,
        elevation_mean_m=artifact_obj.elevation_mean_m,
        bbox=artifact_obj.bbox,
        tile_refs=artifact_obj.tile_refs,
        no_data_ratio=artifact_obj.no_data_ratio,
        artifact_sha256=artifact_obj.artifact_sha256,
        candidate_hash=candidate_obj.heightmap_hash,
        file_sha256=file_digest,
        sha256=file_digest,
        file_path_or_artifact_uri=str(file_path),
        gazebo_dem_file_sha256=gazebo_dem_digest,
        gazebo_dem_file_path_or_artifact_uri=str(gazebo_dem_path),
        generated_at=generated_at,
    )


def build_gazebo_world_candidate(
    *,
    heightmap_file_artifact: TerrainHeightmapFileArtifact | Mapping[str, Any],
    heightmap_artifact: TerrainHeightmapArtifact | Mapping[str, Any],
    heightmap_candidate: TerrainHeightmapCandidate | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    now: datetime | None = None,
) -> GazeboWorldCandidate:
    file_obj = (
        heightmap_file_artifact
        if isinstance(heightmap_file_artifact, TerrainHeightmapFileArtifact)
        else TerrainHeightmapFileArtifact.model_validate(heightmap_file_artifact)
    )
    artifact_obj = (
        heightmap_artifact
        if isinstance(heightmap_artifact, TerrainHeightmapArtifact)
        else TerrainHeightmapArtifact.model_validate(heightmap_artifact)
    )
    candidate_obj = (
        heightmap_candidate
        if isinstance(heightmap_candidate, TerrainHeightmapCandidate)
        else TerrainHeightmapCandidate.model_validate(heightmap_candidate)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    gate_obj = (
        weather_policy_gate
        if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate)
        else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    )
    generated_at = _utc(now)
    file_ref = terrain_heightmap_file_artifact_ref(file_obj)
    artifact_ref = terrain_heightmap_artifact_ref(artifact_obj)
    candidate_ref = terrain_heightmap_candidate_ref(candidate_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    if file_obj.terrain_heightmap_artifact_ref != artifact_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate heightmap artifact ref mismatch"
        )
    if file_obj.terrain_heightmap_candidate_ref != candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate heightmap candidate ref mismatch"
        )
    if artifact_obj.heightmap_candidate_ref != candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate artifact candidate ref mismatch"
        )
    if file_obj.artifact_sha256 != artifact_obj.artifact_sha256:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate heightmap artifact hash mismatch"
        )
    if file_obj.file_sha256 != file_obj.sha256:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate heightmap file hash mismatch"
        )
    if not file_obj.gazebo_dem_file_sha256:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate requires Gazebo DEM file hash"
        )
    if plan_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world candidate route plan weather gate mismatch"
        )

    terrain_scale = (
        round(file_obj.horizontal_resolution_m * (file_obj.pixel_width - 1), 3),
        round(file_obj.horizontal_resolution_m * (file_obj.pixel_height - 1), 3),
        round(file_obj.vertical_scale_m, 3),
    )
    source_refs = _text_tuple((file_ref, *file_obj.source_refs, plan_ref, gate_ref))
    hash_payload = {
        "schema_version": GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION,
        "provider": "digital_twin_fixture_gazebo_world_candidate",
        "source_url": STAGE2_GAZEBO_WORLD_CANDIDATE_SOURCE_URL,
        "world_candidate_status": "generated_for_planning_only",
        "world_format": "gz_sim_world_candidate",
        "heightmap_uri": file_obj.gazebo_dem_file_path_or_artifact_uri,
        "file_sha256": file_obj.gazebo_dem_file_sha256,
        "terrain_scale": terrain_scale,
        "vertical_scale_m": file_obj.vertical_scale_m,
        "bbox": file_obj.bbox,
        "coordinate_frame": file_obj.coordinate_frame,
        "route_plan_status": plan_obj.route_plan_status,
        "weather_policy_gate_status": gate_obj.gate_status,
        "execution_binding_allowed": False,
        "gazebo_world_materialized": False,
        "gazebo_execution_invoked": False,
        "coordinate_transform_generated": False,
        "px4_mission_items_generated": False,
        "sitl_execution_bound": False,
    }
    digest = _content_hash(hash_payload)
    return GazeboWorldCandidate(
        world_candidate_id=_stable_id(
            "gazebo_world_candidate",
            {
                "schema_version": GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION,
                "world_candidate_sha256": digest,
            },
        ),
        terrain_heightmap_file_artifact_ref=file_ref,
        terrain_heightmap_artifact_ref=artifact_ref,
        terrain_heightmap_candidate_ref=candidate_ref,
        digital_twin_route_plan_ref=plan_ref,
        weather_environment_policy_gate_ref=gate_ref,
        source_refs=source_refs,
        source_url=STAGE2_GAZEBO_WORLD_CANDIDATE_SOURCE_URL,
        heightmap_uri=file_obj.gazebo_dem_file_path_or_artifact_uri,
        file_sha256=file_obj.gazebo_dem_file_sha256,
        terrain_scale=terrain_scale,
        vertical_scale_m=file_obj.vertical_scale_m,
        bbox=file_obj.bbox,
        route_plan_status=plan_obj.route_plan_status,
        weather_policy_gate_status=gate_obj.gate_status,
        world_candidate_sha256=digest,
        sha256=digest,
        generated_at=generated_at,
    )


def build_gazebo_world_artifact(
    *,
    gazebo_world_candidate: GazeboWorldCandidate | Mapping[str, Any],
    heightmap_file_artifact: TerrainHeightmapFileArtifact | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    now: datetime | None = None,
    file_root: str | Path | None = None,
) -> GazeboWorldArtifact:
    candidate_obj = (
        gazebo_world_candidate
        if isinstance(gazebo_world_candidate, GazeboWorldCandidate)
        else GazeboWorldCandidate.model_validate(gazebo_world_candidate)
    )
    file_obj = (
        heightmap_file_artifact
        if isinstance(heightmap_file_artifact, TerrainHeightmapFileArtifact)
        else TerrainHeightmapFileArtifact.model_validate(heightmap_file_artifact)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    gate_obj = (
        weather_policy_gate
        if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate)
        else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    )
    generated_at = _utc(now)
    candidate_ref = gazebo_world_candidate_ref(candidate_obj)
    file_ref = terrain_heightmap_file_artifact_ref(file_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    if candidate_obj.terrain_heightmap_file_artifact_ref != file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact heightmap file artifact ref mismatch"
        )
    if candidate_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact route plan ref mismatch"
        )
    if candidate_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact weather gate ref mismatch"
        )
    if candidate_obj.heightmap_uri != file_obj.gazebo_dem_file_path_or_artifact_uri:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact heightmap URI mismatch"
        )
    if candidate_obj.file_sha256 != file_obj.gazebo_dem_file_sha256:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact heightmap file hash mismatch"
        )
    if plan_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "gazebo world artifact route plan weather gate mismatch"
        )

    world_bytes = _deterministic_gazebo_world_sdf(
        world_candidate=candidate_obj,
    )
    world_digest = sha256(world_bytes).hexdigest()
    root = Path(file_root) if file_root is not None else GAZEBO_WORLD_ARTIFACT_ROOT
    world_path = root / f"{world_digest}.world.sdf"
    world_path.parent.mkdir(parents=True, exist_ok=True)
    if not world_path.exists() or world_path.read_bytes() != world_bytes:
        world_path.write_bytes(world_bytes)

    source_refs = _text_tuple((candidate_ref, *candidate_obj.source_refs))
    return GazeboWorldArtifact(
        world_artifact_id=_stable_id(
            "gazebo_world_artifact",
            {
                "schema_version": GAZEBO_WORLD_ARTIFACT_SCHEMA_VERSION,
                "world_file_sha256": world_digest,
            },
        ),
        gazebo_world_candidate_ref=candidate_ref,
        terrain_heightmap_file_artifact_ref=file_ref,
        terrain_heightmap_artifact_ref=candidate_obj.terrain_heightmap_artifact_ref,
        terrain_heightmap_candidate_ref=candidate_obj.terrain_heightmap_candidate_ref,
        digital_twin_route_plan_ref=plan_ref,
        weather_environment_policy_gate_ref=gate_ref,
        source_refs=source_refs,
        source_url=STAGE2_GAZEBO_WORLD_ARTIFACT_SOURCE_URL,
        world_file_path_or_artifact_uri=str(world_path),
        world_file_sha256=world_digest,
        heightmap_uri=candidate_obj.heightmap_uri,
        heightmap_file_sha256=candidate_obj.file_sha256,
        terrain_scale=candidate_obj.terrain_scale,
        vertical_scale_m=candidate_obj.vertical_scale_m,
        bbox=candidate_obj.bbox,
        route_plan_status=candidate_obj.route_plan_status,
        weather_policy_gate_status=candidate_obj.weather_policy_gate_status,
        sha256=world_digest,
        generated_at=generated_at,
    )


def build_coordinate_transform_candidate(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    gazebo_world_candidate: GazeboWorldCandidate | Mapping[str, Any],
    heightmap_file_artifact: TerrainHeightmapFileArtifact | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    now: datetime | None = None,
) -> CoordinateTransformCandidate:
    world_artifact_obj = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    world_candidate_obj = (
        gazebo_world_candidate
        if isinstance(gazebo_world_candidate, GazeboWorldCandidate)
        else GazeboWorldCandidate.model_validate(gazebo_world_candidate)
    )
    file_obj = (
        heightmap_file_artifact
        if isinstance(heightmap_file_artifact, TerrainHeightmapFileArtifact)
        else TerrainHeightmapFileArtifact.model_validate(heightmap_file_artifact)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    generated_at = _utc(now)
    world_artifact_ref = gazebo_world_artifact_ref(world_artifact_obj)
    world_candidate_ref = gazebo_world_candidate_ref(world_candidate_obj)
    file_ref = terrain_heightmap_file_artifact_ref(file_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    if world_artifact_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate world candidate ref mismatch"
        )
    if world_artifact_obj.terrain_heightmap_file_artifact_ref != file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate heightmap file artifact ref mismatch"
        )
    if world_artifact_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate route plan ref mismatch"
        )
    if world_candidate_obj.terrain_heightmap_file_artifact_ref != file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate world candidate heightmap ref mismatch"
        )
    if world_candidate_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate world candidate route plan mismatch"
        )
    if (
        file_obj.gazebo_dem_file_path_or_artifact_uri
        != world_artifact_obj.heightmap_uri
    ):
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate heightmap URI mismatch"
        )
    if geocode_obj.candidate_status != "resolved_fixture_candidate":
        raise DigitalTwinMissionEnvironmentError(
            "coordinate transform candidate requires resolved geocode candidate"
        )
    origin_altitude_m = (
        geocode_obj.altitude_m if geocode_obj.altitude_m is not None else 0.0
    )
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = round(
        meters_per_degree_lat * math.cos(math.radians(geocode_obj.latitude)),
        3,
    )
    source_refs = _text_tuple(
        (
            world_artifact_ref,
            *world_artifact_obj.source_refs,
            geocode_ref,
            *geocode_obj.source_refs,
        )
    )
    hash_payload = {
        "schema_version": COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_artifact_ref,
        "gazebo_world_candidate_ref": world_candidate_ref,
        "terrain_heightmap_file_artifact_ref": file_ref,
        "digital_twin_route_plan_ref": plan_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_coordinate_transform_candidate",
        "source_url": STAGE2_COORDINATE_TRANSFORM_CANDIDATE_SOURCE_URL,
        "transform_candidate_status": "candidate_generated",
        "coordinate_frame_source": "wgs84",
        "coordinate_frame_target": "gazebo_world_local",
        "origin_latitude": geocode_obj.latitude,
        "origin_longitude": geocode_obj.longitude,
        "origin_altitude_m": origin_altitude_m,
        "world_origin_x_m": 0.0,
        "world_origin_y_m": 0.0,
        "world_origin_z_m": 0.0,
        "meters_per_degree_lat": meters_per_degree_lat,
        "meters_per_degree_lon": meters_per_degree_lon,
        "terrain_scale": world_artifact_obj.terrain_scale,
        "bbox": world_artifact_obj.bbox,
        "route_plan_status": plan_obj.route_plan_status,
        "gazebo_world_materialized": True,
        "coordinate_transform_materialized": False,
        "execution_binding_allowed": False,
        "px4_mission_items_generated": False,
        "sitl_execution_bound": False,
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    digest = _content_hash(hash_payload)
    return CoordinateTransformCandidate(
        transform_candidate_id=_stable_id(
            "coordinate_transform_candidate",
            {
                "schema_version": COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION,
                "transform_hash": digest,
            },
        ),
        gazebo_world_artifact_ref=world_artifact_ref,
        gazebo_world_candidate_ref=world_candidate_ref,
        terrain_heightmap_file_artifact_ref=file_ref,
        digital_twin_route_plan_ref=plan_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        source_refs=source_refs,
        source_url=STAGE2_COORDINATE_TRANSFORM_CANDIDATE_SOURCE_URL,
        origin_latitude=geocode_obj.latitude,
        origin_longitude=geocode_obj.longitude,
        origin_altitude_m=origin_altitude_m,
        world_origin_x_m=0.0,
        world_origin_y_m=0.0,
        world_origin_z_m=0.0,
        meters_per_degree_lat=meters_per_degree_lat,
        meters_per_degree_lon=meters_per_degree_lon,
        terrain_scale=world_artifact_obj.terrain_scale,
        bbox=world_artifact_obj.bbox,
        route_plan_status=plan_obj.route_plan_status,
        transform_hash=digest,
        sha256=digest,
        generated_at=generated_at,
    )


def build_digital_twin_px4_mission_item_candidate(
    *,
    mission_anchor_candidate: (
        DigitalTwinMissionAnchorCandidate | Mapping[str, Any] | None
    ),
    coordinate_transform_candidate: CoordinateTransformCandidate | Mapping[str, Any],
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    gazebo_world_candidate: GazeboWorldCandidate | Mapping[str, Any],
    heightmap_file_artifact: TerrainHeightmapFileArtifact | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    vehicle_flight_envelope: VehicleFlightEnvelope | Mapping[str, Any] | None = None,
    require_anchor_terrain_sample: bool = False,
    takeoff_anchor_ref: str = "",
    now: datetime | None = None,
) -> DigitalTwinPx4MissionItemCandidate:
    transform_obj = (
        coordinate_transform_candidate
        if isinstance(coordinate_transform_candidate, CoordinateTransformCandidate)
        else CoordinateTransformCandidate.model_validate(coordinate_transform_candidate)
    )
    world_artifact_obj = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    world_candidate_obj = (
        gazebo_world_candidate
        if isinstance(gazebo_world_candidate, GazeboWorldCandidate)
        else GazeboWorldCandidate.model_validate(gazebo_world_candidate)
    )
    file_obj = (
        heightmap_file_artifact
        if isinstance(heightmap_file_artifact, TerrainHeightmapFileArtifact)
        else TerrainHeightmapFileArtifact.model_validate(heightmap_file_artifact)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    gate_obj = (
        weather_policy_gate
        if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate)
        else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    vehicle_envelope_obj = (
        vehicle_flight_envelope
        if isinstance(vehicle_flight_envelope, VehicleFlightEnvelope)
        else (
            VehicleFlightEnvelope.model_validate(vehicle_flight_envelope)
            if vehicle_flight_envelope
            else None
        )
    )
    anchor_obj = (
        mission_anchor_candidate
        if isinstance(mission_anchor_candidate, DigitalTwinMissionAnchorCandidate)
        else (
            DigitalTwinMissionAnchorCandidate.model_validate(mission_anchor_candidate)
            if mission_anchor_candidate
            else None
        )
    )
    generated_at = _utc(now)
    mission_anchor_ref = (
        digital_twin_mission_anchor_candidate_ref(anchor_obj) if anchor_obj else ""
    )
    transform_ref = coordinate_transform_candidate_ref(transform_obj)
    world_artifact_ref = gazebo_world_artifact_ref(world_artifact_obj)
    world_candidate_ref = gazebo_world_candidate_ref(world_candidate_obj)
    file_ref = terrain_heightmap_file_artifact_ref(file_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    vehicle_envelope_ref = (
        vehicle_flight_envelope_ref(vehicle_envelope_obj)
        if vehicle_envelope_obj
        else ""
    )
    if transform_obj.gazebo_world_artifact_ref != world_artifact_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate world artifact ref mismatch"
        )
    if transform_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate world candidate ref mismatch"
        )
    if transform_obj.terrain_heightmap_file_artifact_ref != file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate heightmap file artifact ref mismatch"
        )
    if transform_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate route plan ref mismatch"
        )
    if transform_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate geocode ref mismatch"
        )
    if world_artifact_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate weather gate ref mismatch"
        )
    if plan_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate route plan weather gate mismatch"
        )
    if geocode_obj.candidate_status != "resolved_fixture_candidate":
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate requires resolved geocode candidate"
        )
    if (
        vehicle_envelope_obj
        and vehicle_envelope_obj.weather_environment_policy_gate_ref != gate_ref
    ):
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate vehicle envelope weather gate mismatch"
        )
    if anchor_obj:
        if anchor_obj.coordinate_transform_candidate_ref != transform_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate anchor transform ref mismatch"
            )
        if anchor_obj.gazebo_world_artifact_ref != world_artifact_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate anchor world artifact ref mismatch"
            )
        if anchor_obj.gazebo_world_candidate_ref != world_candidate_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate anchor world candidate ref mismatch"
            )
        if anchor_obj.digital_twin_route_plan_ref != plan_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate anchor route plan ref mismatch"
            )
        if anchor_obj.real_world_geocode_candidate_ref != geocode_ref:
            raise DigitalTwinMissionEnvironmentError(
                "PX4 mission item candidate anchor geocode ref mismatch"
            )

    override_anchor_ref = _clean_text(takeoff_anchor_ref)
    if (
        anchor_obj
        and override_anchor_ref
        and override_anchor_ref != anchor_obj.takeoff_anchor_ref
    ):
        raise DigitalTwinMissionEnvironmentError(
            "PX4 mission item candidate takeoff anchor must match anchor candidate"
        )
    anchor_ref = _clean_text(
        anchor_obj.takeoff_anchor_ref if anchor_obj else override_anchor_ref
    )
    dropoff_ref = _clean_text(
        anchor_obj.dropoff_anchor_ref if anchor_obj else geocode_ref
    )
    blocked_reasons: list[str] = []
    takeoff_terrain_elevation_m, terrain_sampling_mode = (
        _sample_heightmap_file_at_wgs84(
            file_obj,
            latitude=(
                anchor_obj.takeoff_anchor_latitude_deg if anchor_obj else None
            ),
            longitude=(
                anchor_obj.takeoff_anchor_longitude_deg if anchor_obj else None
            ),
            require_anchor_point_sample=require_anchor_terrain_sample,
        )
    )
    target_terrain_elevation_m: float | None = None
    if require_anchor_terrain_sample:
        target_terrain_elevation_m, _ = _sample_heightmap_file_at_wgs84(
            file_obj,
            latitude=geocode_obj.latitude,
            longitude=geocode_obj.longitude,
            require_anchor_point_sample=True,
        )
    takeoff_agl_margin_m = TAKEOFF_AGL_MARGIN_M
    vehicle_max_takeoff_altitude_m = (
        float(vehicle_envelope_obj.max_takeoff_altitude_m)
        if vehicle_envelope_obj
        else None
    )
    takeoff_altitude_m = round(
        takeoff_terrain_elevation_m + takeoff_agl_margin_m,
        3,
    )
    waypoint_altitude_m = round(float(geocode_obj.altitude_m), 3)
    if target_terrain_elevation_m is not None:
        waypoint_altitude_m = round(
            max(
                waypoint_altitude_m,
                target_terrain_elevation_m + takeoff_agl_margin_m,
            ),
            3,
        )
    altitude_over_envelope = (
        vehicle_max_takeoff_altitude_m is not None
        and max(takeoff_altitude_m, waypoint_altitude_m)
        > vehicle_max_takeoff_altitude_m
    )
    candidate_items: tuple[dict[str, Any], ...] = ()
    if (
        gate_obj.gate_status == "blocked_for_planning"
        or plan_obj.route_plan_status == "blocked_by_weather_policy_gate"
    ):
        candidate_status = "blocked_by_weather_policy_gate"
        blocked_reasons.append("weather_policy_gate_blocked")
    elif (
        not anchor_ref
        or not anchor_obj
        or anchor_obj.takeoff_anchor_latitude_deg is None
        or anchor_obj.takeoff_anchor_longitude_deg is None
        or anchor_obj.takeoff_anchor_altitude_m_agl is None
    ):
        candidate_status = "blocked_by_missing_takeoff_anchor"
    elif altitude_over_envelope:
        candidate_status = "blocked_by_altitude_over_envelope"
        blocked_reasons.append("altitude_over_vehicle_envelope")
    else:
        candidate_status = "candidate_generated_for_planning_only"
        takeoff_latitude = anchor_obj.takeoff_anchor_latitude_deg
        takeoff_longitude = anchor_obj.takeoff_anchor_longitude_deg
        candidate_items = (
            {
                "seq": 0,
                "command": "NAV_TAKEOFF",
                "anchor_ref": anchor_ref,
                "coordinate_frame": "wgs84",
                "latitude_deg": takeoff_latitude,
                "longitude_deg": takeoff_longitude,
                "altitude_m": takeoff_altitude_m,
                "frame": "gazebo_world_local",
                "candidate_only": True,
            },
            {
                "seq": 1,
                "command": "NAV_WAYPOINT",
                "target_ref": dropoff_ref,
                "coordinate_frame": "wgs84",
                "latitude_deg": geocode_obj.latitude,
                "longitude_deg": geocode_obj.longitude,
                "altitude_m": waypoint_altitude_m,
                "frame": "gazebo_world_local",
                "candidate_only": True,
            },
            {
                "seq": 2,
                "command": "NAV_LAND",
                "target_ref": dropoff_ref,
                "coordinate_frame": "wgs84",
                "latitude_deg": geocode_obj.latitude,
                "longitude_deg": geocode_obj.longitude,
                "altitude_m": 0.0,
                "frame": "gazebo_world_local",
                "candidate_only": True,
            },
        )
    if not anchor_ref:
        blocked_reasons.append("takeoff_anchor_missing")

    source_refs = _text_tuple(
        (
            mission_anchor_ref,
            *(anchor_obj.source_refs if anchor_obj else ()),
            vehicle_envelope_ref,
            *(vehicle_envelope_obj.source_refs if vehicle_envelope_obj else ()),
            transform_ref,
            *transform_obj.source_refs,
            gate_ref,
            *gate_obj.source_refs,
        )
    )
    hash_payload = {
        "schema_version": DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION,
        "digital_twin_mission_anchor_candidate_ref": mission_anchor_ref,
        "coordinate_transform_candidate_ref": transform_ref,
        "gazebo_world_artifact_ref": world_artifact_ref,
        "gazebo_world_candidate_ref": world_candidate_ref,
        "terrain_heightmap_file_artifact_ref": file_ref,
        "digital_twin_route_plan_ref": plan_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "vehicle_flight_envelope_ref": vehicle_envelope_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_px4_mission_item_candidate",
        "source_url": STAGE2_PX4_MISSION_ITEM_CANDIDATE_SOURCE_URL,
        "candidate_status": candidate_status,
        "candidate_items": candidate_items,
        "candidate_item_count": len(candidate_items),
        "takeoff_anchor_ref": anchor_ref,
        "takeoff_anchor_latitude_deg": (
            anchor_obj.takeoff_anchor_latitude_deg if anchor_obj else None
        ),
        "takeoff_anchor_longitude_deg": (
            anchor_obj.takeoff_anchor_longitude_deg if anchor_obj else None
        ),
        "takeoff_anchor_altitude_m_agl": (
            anchor_obj.takeoff_anchor_altitude_m_agl if anchor_obj else None
        ),
        "dropoff_target_ref": dropoff_ref,
        "route_plan_status": plan_obj.route_plan_status,
        "weather_policy_gate_status": gate_obj.gate_status,
        "coordinate_transform_materialized": False,
        "takeoff_terrain_elevation_m": takeoff_terrain_elevation_m,
        "takeoff_agl_margin_m": takeoff_agl_margin_m,
        "terrain_sampling_mode": terrain_sampling_mode,
        "vehicle_max_takeoff_altitude_m": vehicle_max_takeoff_altitude_m,
        "execution_binding_allowed": False,
        "px4_mission_upload_allowed": False,
        "mavlink_dispatch_performed": False,
        "sitl_execution_bound": False,
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "blocked_reasons": tuple(blocked_reasons),
    }
    digest = _content_hash(hash_payload)
    return DigitalTwinPx4MissionItemCandidate(
        candidate_id=_stable_id(
            "digital_twin_px4_mission_item_candidate",
            {
                "schema_version": (
                    DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION
                ),
                "mission_item_candidate_hash": digest,
            },
        ),
        digital_twin_mission_anchor_candidate_ref=mission_anchor_ref,
        coordinate_transform_candidate_ref=transform_ref,
        gazebo_world_artifact_ref=world_artifact_ref,
        gazebo_world_candidate_ref=world_candidate_ref,
        terrain_heightmap_file_artifact_ref=file_ref,
        digital_twin_route_plan_ref=plan_ref,
        weather_environment_policy_gate_ref=gate_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        vehicle_flight_envelope_ref=vehicle_envelope_ref,
        source_refs=source_refs,
        source_url=STAGE2_PX4_MISSION_ITEM_CANDIDATE_SOURCE_URL,
        candidate_status=candidate_status,  # type: ignore[arg-type]
        candidate_items=candidate_items,
        candidate_item_count=len(candidate_items),
        takeoff_anchor_ref=anchor_ref,
        takeoff_anchor_latitude_deg=(
            anchor_obj.takeoff_anchor_latitude_deg if anchor_obj else None
        ),
        takeoff_anchor_longitude_deg=(
            anchor_obj.takeoff_anchor_longitude_deg if anchor_obj else None
        ),
        takeoff_anchor_altitude_m_agl=(
            anchor_obj.takeoff_anchor_altitude_m_agl if anchor_obj else None
        ),
        dropoff_target_ref=dropoff_ref,
        route_plan_status=plan_obj.route_plan_status,
        weather_policy_gate_status=gate_obj.gate_status,
        takeoff_terrain_elevation_m=takeoff_terrain_elevation_m,
        takeoff_agl_margin_m=takeoff_agl_margin_m,
        terrain_sampling_mode=terrain_sampling_mode,
        vehicle_max_takeoff_altitude_m=vehicle_max_takeoff_altitude_m,
        mission_item_candidate_hash=digest,
        sha256=digest,
        blocked_reasons=tuple(blocked_reasons),
        generated_at=generated_at,
    )


def build_digital_twin_mission_anchor_candidate(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    gazebo_world_candidate: GazeboWorldCandidate | Mapping[str, Any],
    coordinate_transform_candidate: CoordinateTransformCandidate | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    geocode_candidate: RealWorldGeocodeCandidate | Mapping[str, Any],
    explicit_takeoff_latitude: int | float | None = None,
    explicit_takeoff_longitude: int | float | None = None,
    now: datetime | None = None,
) -> DigitalTwinMissionAnchorCandidate:
    world_artifact_obj = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    world_candidate_obj = (
        gazebo_world_candidate
        if isinstance(gazebo_world_candidate, GazeboWorldCandidate)
        else GazeboWorldCandidate.model_validate(gazebo_world_candidate)
    )
    transform_obj = (
        coordinate_transform_candidate
        if isinstance(coordinate_transform_candidate, CoordinateTransformCandidate)
        else CoordinateTransformCandidate.model_validate(coordinate_transform_candidate)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    geocode_obj = (
        geocode_candidate
        if isinstance(geocode_candidate, RealWorldGeocodeCandidate)
        else RealWorldGeocodeCandidate.model_validate(geocode_candidate)
    )
    generated_at = _utc(now)
    world_artifact_ref = gazebo_world_artifact_ref(world_artifact_obj)
    world_candidate_ref = gazebo_world_candidate_ref(world_candidate_obj)
    transform_ref = coordinate_transform_candidate_ref(transform_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    geocode_ref = real_world_geocode_candidate_ref(geocode_obj)
    if world_artifact_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate world candidate ref mismatch"
        )
    if world_artifact_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate world route plan mismatch"
        )
    if transform_obj.gazebo_world_artifact_ref != world_artifact_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate transform world artifact ref mismatch"
        )
    if transform_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate transform world candidate ref mismatch"
        )
    if transform_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate transform route plan mismatch"
        )
    if transform_obj.real_world_geocode_candidate_ref != geocode_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate transform geocode ref mismatch"
        )
    if geocode_obj.candidate_status != "resolved_fixture_candidate":
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin anchor candidate requires resolved geocode candidate"
        )

    blocked_reasons: list[str] = []
    requested_distance_m = max(float(plan_obj.requested_distance_m), 1.0)
    explicit_takeoff_requested = (
        explicit_takeoff_latitude is not None
        and explicit_takeoff_longitude is not None
    )
    if explicit_takeoff_requested:
        planned_takeoff_latitude = round(float(explicit_takeoff_latitude), 7)
        planned_takeoff_longitude = round(float(explicit_takeoff_longitude), 7)
    else:
        planned_takeoff_latitude, planned_takeoff_longitude = _wgs84_destination(
            origin_latitude=geocode_obj.latitude,
            origin_longitude=geocode_obj.longitude,
            bearing_deg=TAKEOFF_FROM_TARGET_BEARING_DEG,
            distance_m=requested_distance_m,
        )

    if plan_obj.route_plan_status == "blocked_by_weather_policy_gate":
        anchor_candidate_status = "blocked_by_weather_policy_gate"
        blocked_reasons.append("weather_policy_gate_blocked")
    elif plan_obj.route_plan_status == "blocked_by_route_feasibility":
        anchor_candidate_status = "blocked_by_route_plan"
        blocked_reasons.append("route_plan_blocked")
    elif not _wgs84_inside_bbox(
        latitude=planned_takeoff_latitude,
        longitude=planned_takeoff_longitude,
        bbox=world_artifact_obj.bbox,
    ):
        anchor_candidate_status = "blocked_by_route_plan"
        blocked_reasons.append("takeoff_anchor_outside_world_bbox")
    else:
        anchor_candidate_status = "anchors_available_for_planning"

    takeoff_anchor_ref = (
        f"digital_twin_fixture_anchor:{geocode_obj.resolved_location_label}:takeoff"
        if anchor_candidate_status == "anchors_available_for_planning"
        else ""
    )
    takeoff_anchor_latitude_deg = (
        planned_takeoff_latitude
        if anchor_candidate_status == "anchors_available_for_planning"
        else None
    )
    takeoff_anchor_longitude_deg = (
        planned_takeoff_longitude
        if anchor_candidate_status == "anchors_available_for_planning"
        else None
    )
    takeoff_anchor_altitude_m_agl = (
        15.0 if anchor_candidate_status == "anchors_available_for_planning" else None
    )
    dropoff_anchor_ref = geocode_ref
    dropoff_anchor_latitude_deg = (
        round(geocode_obj.latitude, 7)
        if anchor_candidate_status == "anchors_available_for_planning"
        else None
    )
    dropoff_anchor_longitude_deg = (
        round(geocode_obj.longitude, 7)
        if anchor_candidate_status == "anchors_available_for_planning"
        else None
    )
    source_refs = _text_tuple(
        (
            world_artifact_ref,
            world_candidate_ref,
            transform_ref,
            plan_ref,
            geocode_ref,
            *world_artifact_obj.source_refs,
            *world_candidate_obj.source_refs,
            *transform_obj.source_refs,
            *plan_obj.source_refs,
            *geocode_obj.source_refs,
        )
    )
    hash_payload = {
        "schema_version": DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_artifact_ref,
        "gazebo_world_candidate_ref": world_candidate_ref,
        "coordinate_transform_candidate_ref": transform_ref,
        "digital_twin_route_plan_ref": plan_ref,
        "real_world_geocode_candidate_ref": geocode_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_mission_anchor_candidate",
        "source_url": STAGE2_MISSION_ANCHOR_CANDIDATE_SOURCE_URL,
        "anchor_candidate_status": anchor_candidate_status,
        "anchor_mode": (
            "operator_coordinate_pair_anchor"
            if explicit_takeoff_requested
            else "fixture_or_manual_digital_twin_anchor"
        ),
        "takeoff_anchor_ref": takeoff_anchor_ref,
        "takeoff_anchor_latitude_deg": takeoff_anchor_latitude_deg,
        "takeoff_anchor_longitude_deg": takeoff_anchor_longitude_deg,
        "takeoff_anchor_altitude_m_agl": takeoff_anchor_altitude_m_agl,
        "dropoff_anchor_ref": dropoff_anchor_ref,
        "dropoff_anchor_latitude_deg": dropoff_anchor_latitude_deg,
        "dropoff_anchor_longitude_deg": dropoff_anchor_longitude_deg,
        "takeoff_from_target_bearing_deg": TAKEOFF_FROM_TARGET_BEARING_DEG,
        "explicit_takeoff_anchor_provided": explicit_takeoff_requested,
        "takeoff_to_dropoff_distance_m": (
            round(
                _haversine_distance_m(
                    latitude_a=takeoff_anchor_latitude_deg,
                    longitude_a=takeoff_anchor_longitude_deg,
                    latitude_b=dropoff_anchor_latitude_deg,
                    longitude_b=dropoff_anchor_longitude_deg,
                ),
                3,
            )
            if (
                takeoff_anchor_latitude_deg is not None
                and takeoff_anchor_longitude_deg is not None
                and dropoff_anchor_latitude_deg is not None
                and dropoff_anchor_longitude_deg is not None
            )
            else None
        ),
        "route_plan_status": plan_obj.route_plan_status,
        "coordinate_transform_materialized": False,
        "execution_binding_allowed": False,
        "px4_mission_upload_allowed": False,
        "mavlink_dispatch_performed": False,
        "sitl_execution_bound": False,
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "blocked_reasons": tuple(blocked_reasons),
    }
    digest = _content_hash(hash_payload)
    return DigitalTwinMissionAnchorCandidate(
        anchor_candidate_id=_stable_id(
            "digital_twin_mission_anchor_candidate",
            {
                "schema_version": (
                    DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION
                ),
                "anchor_hash": digest,
            },
        ),
        gazebo_world_artifact_ref=world_artifact_ref,
        gazebo_world_candidate_ref=world_candidate_ref,
        coordinate_transform_candidate_ref=transform_ref,
        digital_twin_route_plan_ref=plan_ref,
        real_world_geocode_candidate_ref=geocode_ref,
        source_refs=source_refs,
        source_url=STAGE2_MISSION_ANCHOR_CANDIDATE_SOURCE_URL,
        anchor_candidate_status=anchor_candidate_status,  # type: ignore[arg-type]
        anchor_mode=(
            "operator_coordinate_pair_anchor"
            if explicit_takeoff_requested
            else "fixture_or_manual_digital_twin_anchor"
        ),
        takeoff_anchor_ref=(
            f"operator_coordinate_pair:{geocode_obj.resolved_location_label}:takeoff"
            if explicit_takeoff_requested and takeoff_anchor_ref
            else takeoff_anchor_ref
        ),
        takeoff_anchor_latitude_deg=takeoff_anchor_latitude_deg,
        takeoff_anchor_longitude_deg=takeoff_anchor_longitude_deg,
        takeoff_anchor_altitude_m_agl=takeoff_anchor_altitude_m_agl,
        dropoff_anchor_ref=dropoff_anchor_ref,
        dropoff_anchor_latitude_deg=dropoff_anchor_latitude_deg,
        dropoff_anchor_longitude_deg=dropoff_anchor_longitude_deg,
        route_plan_status=plan_obj.route_plan_status,
        anchor_hash=digest,
        sha256=digest,
        blocked_reasons=tuple(blocked_reasons),
        generated_at=generated_at,
    )


def build_digital_twin_sitl_binding_gate(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    coordinate_transform_candidate: CoordinateTransformCandidate | Mapping[str, Any],
    px4_mission_item_candidate: DigitalTwinPx4MissionItemCandidate
    | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    route_plan: DigitalTwinRoutePlan | Mapping[str, Any],
    now: datetime | None = None,
) -> DigitalTwinSITLBindingGate:
    world_artifact_obj = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    transform_obj = (
        coordinate_transform_candidate
        if isinstance(coordinate_transform_candidate, CoordinateTransformCandidate)
        else CoordinateTransformCandidate.model_validate(coordinate_transform_candidate)
    )
    mission_item_obj = (
        px4_mission_item_candidate
        if isinstance(px4_mission_item_candidate, DigitalTwinPx4MissionItemCandidate)
        else DigitalTwinPx4MissionItemCandidate.model_validate(
            px4_mission_item_candidate
        )
    )
    gate_obj = (
        weather_policy_gate
        if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate)
        else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    )
    plan_obj = (
        route_plan
        if isinstance(route_plan, DigitalTwinRoutePlan)
        else DigitalTwinRoutePlan.model_validate(route_plan)
    )
    evaluated_at = _utc(now)
    world_artifact_ref = gazebo_world_artifact_ref(world_artifact_obj)
    transform_ref = coordinate_transform_candidate_ref(transform_obj)
    mission_item_ref = digital_twin_px4_mission_item_candidate_ref(mission_item_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    plan_ref = digital_twin_route_plan_ref(plan_obj)
    world_candidate_ref = world_artifact_obj.gazebo_world_candidate_ref
    heightmap_file_ref = world_artifact_obj.terrain_heightmap_file_artifact_ref
    if transform_obj.gazebo_world_artifact_ref != world_artifact_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate world artifact ref mismatch"
        )
    if transform_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate transform world candidate ref mismatch"
        )
    if transform_obj.terrain_heightmap_file_artifact_ref != heightmap_file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate transform heightmap file ref mismatch"
        )
    if transform_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate transform route plan mismatch"
        )
    if world_artifact_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate world route plan mismatch"
        )
    if world_artifact_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate world weather gate mismatch"
        )
    if plan_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate route weather gate mismatch"
        )
    if mission_item_obj.coordinate_transform_candidate_ref != transform_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item transform ref mismatch"
        )
    if mission_item_obj.gazebo_world_artifact_ref != world_artifact_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item world artifact ref mismatch"
        )
    if mission_item_obj.gazebo_world_candidate_ref != world_candidate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item world candidate ref mismatch"
        )
    if mission_item_obj.terrain_heightmap_file_artifact_ref != heightmap_file_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item heightmap file ref mismatch"
        )
    if mission_item_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item weather gate mismatch"
        )
    if mission_item_obj.digital_twin_route_plan_ref != plan_ref:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin SITL binding gate mission item route plan mismatch"
        )

    blocked_reasons: list[str] = []
    if gate_obj.gate_status == "blocked_for_planning":
        blocked_reasons.append("weather_policy_gate_blocked")
    if plan_obj.route_plan_status == "blocked_by_route_feasibility":
        blocked_reasons.append("route_plan_blocked")
    if mission_item_obj.candidate_status == "blocked_by_weather_policy_gate":
        blocked_reasons.append("weather_policy_gate_blocked")
    if mission_item_obj.candidate_status == "blocked_by_missing_takeoff_anchor":
        blocked_reasons.append("takeoff_anchor_missing")
    if mission_item_obj.candidate_status == "blocked_by_altitude_over_envelope":
        blocked_reasons.append("altitude_over_vehicle_envelope")
    if "takeoff_anchor_missing" in set(mission_item_obj.blocked_reasons):
        blocked_reasons.append("takeoff_anchor_missing")
    if (
        mission_item_obj.candidate_status != "candidate_generated_for_planning_only"
        and not mission_item_obj.candidate_status.startswith("blocked_")
    ):
        blocked_reasons.append("px4_mission_item_candidate_not_generated")
    if (
        mission_item_obj.candidate_status == "candidate_generated_for_planning_only"
        and mission_item_obj.candidate_item_count <= 0
    ):
        blocked_reasons.append("px4_mission_item_candidate_empty")
    blocked_reasons_tuple = _text_tuple(blocked_reasons)
    binding_eligible = not blocked_reasons_tuple
    binding_allowed = False
    binding_gate_status = (
        "eligible_for_operator_approved_sitl_binding"
        if binding_eligible
        else "blocked"
    )
    source_refs = _text_tuple(
        (
            world_artifact_ref,
            transform_ref,
            mission_item_ref,
            gate_ref,
            plan_ref,
            *world_artifact_obj.source_refs,
            *transform_obj.source_refs,
            *mission_item_obj.source_refs,
            *gate_obj.source_refs,
            *plan_obj.source_refs,
        )
    )
    hash_payload = {
        "schema_version": DIGITAL_TWIN_SITL_BINDING_GATE_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_artifact_ref,
        "coordinate_transform_candidate_ref": transform_ref,
        "digital_twin_px4_mission_item_candidate_ref": mission_item_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "digital_twin_route_plan_ref": plan_ref,
        "source_refs": source_refs,
        "provider": "digital_twin_fixture_sitl_binding_gate",
        "source_url": STAGE2_SITL_BINDING_GATE_SOURCE_URL,
        "binding_gate_status": binding_gate_status,
        "binding_mode": "operator_approved_simulation_only",
        "binding_allowed": binding_allowed,
        "binding_eligible": binding_eligible,
        "operator_approval_required": True,
        "server_opt_in_required": True,
        "simulation_only": True,
        "observed_facts_only": True,
        "route_plan_status": plan_obj.route_plan_status,
        "weather_policy_gate_status": gate_obj.gate_status,
        "px4_mission_item_candidate_status": mission_item_obj.candidate_status,
        "candidate_item_count": mission_item_obj.candidate_item_count,
        "coordinate_transform_materialized": False,
        "px4_mission_upload_allowed": False,
        "mavlink_dispatch_performed": False,
        "sitl_execution_bound": False,
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "blocked_reasons": blocked_reasons_tuple,
    }
    digest = _content_hash(hash_payload)
    return DigitalTwinSITLBindingGate(
        gate_id=_stable_id(
            "digital_twin_sitl_binding_gate",
            {
                "schema_version": DIGITAL_TWIN_SITL_BINDING_GATE_SCHEMA_VERSION,
                "binding_gate_hash": digest,
            },
        ),
        gazebo_world_artifact_ref=world_artifact_ref,
        coordinate_transform_candidate_ref=transform_ref,
        digital_twin_px4_mission_item_candidate_ref=mission_item_ref,
        weather_environment_policy_gate_ref=gate_ref,
        digital_twin_route_plan_ref=plan_ref,
        source_refs=source_refs,
        source_url=STAGE2_SITL_BINDING_GATE_SOURCE_URL,
        binding_gate_status=binding_gate_status,  # type: ignore[arg-type]
        binding_allowed=binding_allowed,
        binding_eligible=binding_eligible,
        route_plan_status=plan_obj.route_plan_status,
        weather_policy_gate_status=gate_obj.gate_status,
        px4_mission_item_candidate_status=mission_item_obj.candidate_status,
        candidate_item_count=mission_item_obj.candidate_item_count,
        blocked_reasons=blocked_reasons_tuple,
        binding_gate_hash=digest,
        sha256=digest,
        evaluated_at=evaluated_at,
    )


def build_weather_environment_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    weather_hazard_labels: Sequence[str],
    now: datetime | None = None,
) -> WeatherEnvironmentSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    retrieved_at = _utc(now)
    hazards = {str(item) for item in weather_hazard_labels}
    rainy = "rain_or_storm" in hazards
    precipitation_label: Literal["none", "rain_or_storm"] = (
        "rain_or_storm" if rainy else "none"
    )
    hash_payload = {
        "real_world_mission_target_ref": real_world_mission_target_ref(target_obj),
        "provider": "deterministic_prompt_weather_parser",
        "source_url": STAGE1_WEATHER_SOURCE_URL,
        "valid_at": retrieved_at.isoformat(),
        "location_label": target_obj.resolved_location_label,
        "rain_or_precipitation": rainy,
        "precipitation_label": precipitation_label,
        "forecast_or_observed": "operator_prompt_constraint",
        "stale_or_missing_external_weather": True,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "retrieved_at": retrieved_at.isoformat(),
        "weather_hash": digest,
    }
    return WeatherEnvironmentSnapshot(
        snapshot_id=_stable_id(
            "weather_environment_snapshot",
            snapshot_id_payload,
        ),
        real_world_mission_target_ref=real_world_mission_target_ref(target_obj),
        source_url=STAGE1_WEATHER_SOURCE_URL,
        source_refs=(real_world_mission_target_ref(target_obj),),
        retrieved_at=retrieved_at,
        valid_at=retrieved_at,
        location_label=target_obj.resolved_location_label,
        weather_hash=digest,
        sha256=digest,
        rain_or_precipitation=rainy,
        precipitation_label=precipitation_label,
    )


def build_weather_source_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    target_resolution: RealWorldTargetResolution | Mapping[str, Any],
    now: datetime | None = None,
    fetcher: Any | None = None,
    timeout_seconds: float = 20.0,
) -> WeatherSourceSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    resolution_obj = (
        target_resolution
        if isinstance(target_resolution, RealWorldTargetResolution)
        else RealWorldTargetResolution.model_validate(target_resolution)
    )
    captured_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    resolution_ref = real_world_target_resolution_ref(resolution_obj)
    if resolution_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source weather target resolution target mismatch"
        )

    source_url = _open_meteo_jma_url(resolution_obj.latitude, resolution_obj.longitude)
    source_refs = (target_ref, resolution_ref)
    provider_response_status = "not_requested"
    source_unavailable = False
    valid_at: datetime | None = None
    precipitation_mm_per_hour: float | None = None
    wind_speed_mps: float | None = None
    wind_gust_mps: float | None = None
    wind_direction_deg: float | None = None
    visibility_m: float | None = None
    temperature_c: float | None = None
    pressure_hpa: float | None = None
    try:
        _validate_open_meteo_source_url(source_url)
        if fetcher is None:
            request = Request(
                source_url,
                headers={"User-Agent": SOURCE_BACKED_PROVIDER_USER_AGENT},
            )
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                _validate_open_meteo_source_url(getattr(response, "url", source_url))
                provider_response_status = f"http_{getattr(response, 'status', 200)}"
                payload = json.loads(response.read().decode("utf-8"))
        else:
            fetched = fetcher(source_url)
            if isinstance(fetched, tuple):
                provider_response_status = str(fetched[0])
                fetched_payload = fetched[1]
            else:
                provider_response_status = "injected_fetcher"
                fetched_payload = fetched
            payload = (
                json.loads(str(fetched_payload))
                if isinstance(fetched_payload, str)
                else dict(fetched_payload)
            )
        current = payload.get("current") or {}
        current_time = current.get("time")
        if not current_time:
            raise DigitalTwinMissionEnvironmentError(
                "Open-Meteo response missing current time"
            )
        valid_at = _utc(datetime.fromisoformat(str(current_time).replace("Z", "+00:00")))
        precipitation_mm_per_hour = max(
            0.0,
            _optional_float(current.get("precipitation")) or 0.0,
        )
        wind_speed_kmh = _optional_float(current.get("wind_speed_10m"))
        wind_speed_mps = (
            round(wind_speed_kmh / 3.6, 3) if wind_speed_kmh is not None else None
        )
        wind_gust_kmh = _optional_float(current.get("wind_gusts_10m"))
        wind_gust_mps = (
            round(wind_gust_kmh / 3.6, 3) if wind_gust_kmh is not None else None
        )
        wind_direction_deg = _optional_float(current.get("wind_direction_10m"))
        temperature_c = _optional_float(current.get("temperature_2m"))
        pressure_hpa = _optional_float(current.get("surface_pressure"))
        status: Literal[
            "source_backed_weather_captured",
            "blocked_source_unavailable",
        ] = "source_backed_weather_captured"
        provider: Literal["open_meteo_jma", "source_backed_weather_unavailable"] = (
            "open_meteo_jma"
        )
        source_backed_weather = True
    except (DigitalTwinMissionEnvironmentError, HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        status = "blocked_source_unavailable"
        provider = "source_backed_weather_unavailable"
        if isinstance(exc, HTTPError):
            provider_response_status = f"source_unavailable:http_{exc.code}"
        else:
            provider_response_status = f"source_unavailable:{type(exc).__name__}"
        source_unavailable = True
        source_backed_weather = False

    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "real_world_target_resolution_ref": resolution_ref,
        "source_refs": source_refs,
        "provider": provider,
        "source_url": source_url,
        "snapshot_status": status,
        "coordinate_frame": "wgs84",
        "latitude": resolution_obj.latitude,
        "longitude": resolution_obj.longitude,
        "valid_at": valid_at.isoformat() if valid_at else None,
        "precipitation_mm_per_hour": precipitation_mm_per_hour,
        "wind_speed_mps": wind_speed_mps,
        "wind_gust_mps": wind_gust_mps,
        "wind_direction_deg": wind_direction_deg,
        "visibility_m": visibility_m,
        "temperature_c": temperature_c,
        "pressure_hpa": pressure_hpa,
        "provider_response_status": provider_response_status,
        "source_backed_weather": source_backed_weather,
        "source_unavailable": source_unavailable,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "captured_at": captured_at.isoformat(),
        "weather_hash": digest,
    }
    return WeatherSourceSnapshot(
        snapshot_id=_stable_id("weather_source_snapshot", snapshot_id_payload),
        real_world_mission_target_ref=target_ref,
        real_world_target_resolution_ref=resolution_ref,
        source_refs=source_refs,
        provider=provider,
        source_url=source_url,
        snapshot_status=status,
        latitude=resolution_obj.latitude,
        longitude=resolution_obj.longitude,
        valid_at=valid_at,
        captured_at=captured_at,
        precipitation_mm_per_hour=precipitation_mm_per_hour,
        wind_speed_mps=wind_speed_mps,
        wind_gust_mps=wind_gust_mps,
        wind_direction_deg=wind_direction_deg,
        visibility_m=visibility_m,
        temperature_c=temperature_c,
        pressure_hpa=pressure_hpa,
        provider_response_status=provider_response_status,
        source_backed_weather=source_backed_weather,
        source_unavailable=source_unavailable,
        weather_hash=digest,
        sha256=digest,
    )


def build_weather_environment_snapshot_from_source_snapshot(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    weather_source_snapshot: WeatherSourceSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> WeatherEnvironmentSnapshot:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    source_obj = (
        weather_source_snapshot
        if isinstance(weather_source_snapshot, WeatherSourceSnapshot)
        else WeatherSourceSnapshot.model_validate(weather_source_snapshot)
    )
    retrieved_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    source_ref = weather_source_snapshot_ref(source_obj)
    if source_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "source weather environment target mismatch"
        )
    source_unavailable = source_obj.snapshot_status == "blocked_source_unavailable"
    rainy = bool(
        source_obj.precipitation_mm_per_hour is not None
        and source_obj.precipitation_mm_per_hour > 0
    )
    precipitation_label: Literal["none", "rain_or_storm"] = (
        "rain_or_storm" if rainy else "none"
    )
    forecast_or_observed: Literal[
        "open_meteo_current_conditions",
        "source_weather_unavailable",
    ] = (
        "source_weather_unavailable"
        if source_unavailable
        else "open_meteo_current_conditions"
    )
    provider: Literal["open_meteo_jma_weather", "source_backed_weather_unavailable"] = (
        "source_backed_weather_unavailable"
        if source_unavailable
        else "open_meteo_jma_weather"
    )
    valid_at = source_obj.valid_at or retrieved_at
    source_refs = (target_ref, source_ref)
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "weather_source_snapshot_ref": source_ref,
        "provider": provider,
        "source_url": source_obj.source_url,
        "valid_at": valid_at.isoformat(),
        "location_label": target_obj.resolved_location_label,
        "rain_or_precipitation": rainy,
        "precipitation_label": precipitation_label,
        "precipitation_mm_per_hour": source_obj.precipitation_mm_per_hour,
        "wind_speed_mps": source_obj.wind_speed_mps,
        "wind_gust_mps": source_obj.wind_gust_mps,
        "wind_direction_deg": source_obj.wind_direction_deg,
        "visibility_m": source_obj.visibility_m,
        "temperature_c": source_obj.temperature_c,
        "pressure_hpa": source_obj.pressure_hpa,
        "forecast_or_observed": forecast_or_observed,
        "source_unavailable": source_unavailable,
        "stale_or_missing_external_weather": source_unavailable,
        "source_refs": source_refs,
    }
    digest = _content_hash(hash_payload)
    snapshot_id_payload = {
        **hash_payload,
        "retrieved_at": retrieved_at.isoformat(),
        "weather_hash": digest,
    }
    return WeatherEnvironmentSnapshot(
        snapshot_id=_stable_id(
            "weather_environment_snapshot",
            snapshot_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        weather_source_snapshot_ref=source_ref,
        provider=provider,
        source_url=source_obj.source_url,
        source_refs=source_refs,
        retrieved_at=retrieved_at,
        valid_at=valid_at,
        location_label=target_obj.resolved_location_label,
        weather_hash=digest,
        sha256=digest,
        forecast_or_observed=forecast_or_observed,
        rain_or_precipitation=rainy,
        precipitation_label=precipitation_label,
        precipitation_mm_per_hour=source_obj.precipitation_mm_per_hour,
        wind_speed_mps=source_obj.wind_speed_mps,
        wind_gust_mps=source_obj.wind_gust_mps,
        wind_direction_deg=source_obj.wind_direction_deg,
        visibility_m=source_obj.visibility_m,
        temperature_c=source_obj.temperature_c,
        pressure_hpa=source_obj.pressure_hpa,
        source_unavailable=source_unavailable,
        stale_or_missing_external_weather=source_unavailable,
    )


def build_digital_twin_route_feasibility(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    terrain: TerrainEnvironmentSnapshot
    | TileBackedTerrainEnvironmentSnapshot
    | Mapping[str, Any],
    prompt_projected_terrain: TerrainEnvironmentSnapshot | Mapping[str, Any] | None = None,
    weather: WeatherEnvironmentSnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> DigitalTwinRouteFeasibility:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    terrain_obj: TerrainEnvironmentSnapshot | TileBackedTerrainEnvironmentSnapshot
    if isinstance(terrain, (TerrainEnvironmentSnapshot, TileBackedTerrainEnvironmentSnapshot)):
        terrain_obj = terrain
    elif (
        isinstance(terrain, Mapping)
        and terrain.get("schema_version")
        == TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION
    ):
        terrain_obj = TileBackedTerrainEnvironmentSnapshot.model_validate(terrain)
    else:
        terrain_obj = TerrainEnvironmentSnapshot.model_validate(terrain)
    prompt_terrain_obj = (
        prompt_projected_terrain
        if isinstance(prompt_projected_terrain, TerrainEnvironmentSnapshot)
        else (
            TerrainEnvironmentSnapshot.model_validate(prompt_projected_terrain)
            if prompt_projected_terrain is not None
            else None
        )
    )
    weather_obj = (
        weather
        if isinstance(weather, WeatherEnvironmentSnapshot)
        else WeatherEnvironmentSnapshot.model_validate(weather)
    )
    computed_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    terrain_ref = _terrain_input_ref(terrain_obj)
    is_tile_backed_input = isinstance(terrain_obj, TileBackedTerrainEnvironmentSnapshot)
    input_source: Literal["prompt_projected_terrain", "tile_backed_terrain"] = (
        "tile_backed_terrain" if is_tile_backed_input else "prompt_projected_terrain"
    )
    prompt_terrain_ref = (
        terrain_environment_snapshot_ref(prompt_terrain_obj)
        if prompt_terrain_obj is not None
        else terrain_ref
    )
    tile_terrain_ref = terrain_ref if is_tile_backed_input else ""
    weather_ref = weather_environment_snapshot_ref(weather_obj)
    if terrain_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "route feasibility terrain snapshot target mismatch"
        )
    if is_tile_backed_input:
        if prompt_terrain_obj is None:
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed route feasibility requires prompt-projected terrain"
            )
        if prompt_terrain_obj.real_world_mission_target_ref != target_ref:
            raise DigitalTwinMissionEnvironmentError(
                "route feasibility prompt terrain target mismatch"
            )
    if weather_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "route feasibility weather snapshot target mismatch"
        )

    requested_distance_m = max((target_obj.requested_distance_km or 1.0) * 1000.0, 1.0)
    actual_route_distance_m = requested_distance_m
    elevation_gain_m = max(0.0, terrain_obj.elevation_max_m - terrain_obj.elevation_min_m)
    average_slope_percent = round((elevation_gain_m / actual_route_distance_m) * 100.0, 3)
    max_projected_slope_percent = round(average_slope_percent * 1.4, 3)
    min_terrain_clearance_m = 30.0
    route_risks: list[str] = []
    warnings: list[str] = []
    blocked: list[str] = []

    if target_obj.target_resolution_status == "prompt_target_ambiguous":
        blocked.append("target_location_ambiguous")
        route_risks.append("target_resolution_required")
    elif target_obj.target_resolution_status == "prompt_target_unresolved":
        warnings.append("target_location_requires_geocode_before_execution")
        route_risks.append("target_resolution_required")
    if terrain_obj.slope_risk_label in {"moderate", "high"}:
        warnings.append(f"terrain_slope_{terrain_obj.slope_risk_label}")
        route_risks.append("terrain_slope_risk")
    if terrain_obj.elevation_max_m >= 2500:
        warnings.append("high_elevation_route")
        route_risks.append("high_elevation_density_altitude")
    if weather_obj.rain_or_precipitation:
        warnings.append("rain_or_precipitation_route")
        route_risks.append("wet_weather_visibility_or_surface_risk")
    if target_obj.payload_weight_kg is not None and target_obj.payload_weight_kg >= 3:
        warnings.append("payload_weight_margin_required")
        route_risks.append("payload_energy_margin_risk")
    if actual_route_distance_m >= 10000:
        warnings.append("long_route_margin_required")
        route_risks.append("long_route_energy_margin_risk")

    status: Literal[
        "feasible_for_planning",
        "feasible_with_warnings",
        "blocked_for_planning",
    ]
    if blocked:
        status = "blocked_for_planning"
    elif warnings:
        status = "feasible_with_warnings"
    else:
        status = "feasible_for_planning"

    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "terrain_environment_snapshot_ref": terrain_ref,
        "prompt_projected_terrain_environment_snapshot_ref": prompt_terrain_ref,
        "tile_backed_terrain_environment_snapshot_ref": tile_terrain_ref,
        "weather_environment_snapshot_ref": weather_ref,
        "route_feasibility_input_source": input_source,
        "route_feasibility_status": status,
        "requested_distance_m": round(requested_distance_m, 3),
        "actual_route_distance_m": round(actual_route_distance_m, 3),
        "elevation_min_m": terrain_obj.elevation_min_m,
        "elevation_max_m": terrain_obj.elevation_max_m,
        "elevation_gain_m": round(elevation_gain_m, 3),
        "average_slope_percent": average_slope_percent,
        "max_projected_slope_percent": max_projected_slope_percent,
        "min_terrain_clearance_m": min_terrain_clearance_m,
        "payload_weight_kg": target_obj.payload_weight_kg,
        "rain_or_precipitation": weather_obj.rain_or_precipitation,
        "battery_margin_assumption_percent": 35.0,
        "terrain_risk_label": terrain_obj.slope_risk_label,
        "route_risk_labels": tuple(route_risks),
        "blocked_reasons": tuple(blocked),
        "warning_reasons": tuple(warnings),
        "source_refs": _text_tuple(
            (target_ref, terrain_ref, prompt_terrain_ref, tile_terrain_ref, weather_ref)
        ),
    }
    digest = _content_hash(hash_payload)
    feasibility_id_payload = {
        **hash_payload,
        "computed_at": computed_at.isoformat(),
        "feasibility_hash": digest,
    }
    return DigitalTwinRouteFeasibility(
        feasibility_id=_stable_id(
            "digital_twin_route_feasibility",
            feasibility_id_payload,
        ),
        real_world_mission_target_ref=target_ref,
        terrain_environment_snapshot_ref=terrain_ref,
        prompt_projected_terrain_environment_snapshot_ref=prompt_terrain_ref,
        tile_backed_terrain_environment_snapshot_ref=tile_terrain_ref,
        weather_environment_snapshot_ref=weather_ref,
        source_refs=_text_tuple(
            (target_ref, terrain_ref, prompt_terrain_ref, tile_terrain_ref, weather_ref)
        ),
        route_feasibility_input_source=input_source,
        route_feasibility_status=status,
        requested_distance_m=round(requested_distance_m, 3),
        actual_route_distance_m=round(actual_route_distance_m, 3),
        elevation_min_m=terrain_obj.elevation_min_m,
        elevation_max_m=terrain_obj.elevation_max_m,
        elevation_gain_m=round(elevation_gain_m, 3),
        average_slope_percent=average_slope_percent,
        max_projected_slope_percent=max_projected_slope_percent,
        min_terrain_clearance_m=min_terrain_clearance_m,
        payload_weight_kg=target_obj.payload_weight_kg,
        rain_or_precipitation=weather_obj.rain_or_precipitation,
        battery_margin_assumption_percent=35.0,
        terrain_risk_label=terrain_obj.slope_risk_label,
        route_risk_labels=tuple(route_risks),
        blocked_reasons=tuple(blocked),
        warning_reasons=tuple(warnings),
        feasibility_hash=digest,
        sha256=digest,
        computed_at=computed_at,
    )


def build_weather_environment_policy_gate(
    *,
    weather: WeatherEnvironmentSnapshot | Mapping[str, Any],
    route_feasibility: DigitalTwinRouteFeasibility | Mapping[str, Any],
    now: datetime | None = None,
) -> WeatherEnvironmentPolicyGate:
    weather_obj = (
        weather
        if isinstance(weather, WeatherEnvironmentSnapshot)
        else WeatherEnvironmentSnapshot.model_validate(weather)
    )
    route_obj = (
        route_feasibility
        if isinstance(route_feasibility, DigitalTwinRouteFeasibility)
        else DigitalTwinRouteFeasibility.model_validate(route_feasibility)
    )
    evaluated_at = _utc(now)
    weather_ref = weather_environment_snapshot_ref(weather_obj)
    route_ref = digital_twin_route_feasibility_ref(route_obj)
    if route_obj.weather_environment_snapshot_ref != weather_ref:
        raise DigitalTwinMissionEnvironmentError(
            "weather policy gate route weather mismatch"
        )

    external_weather_required = bool(
        weather_obj.rain_or_precipitation
        or weather_obj.weather_source_snapshot_ref
        or weather_obj.source_unavailable
    )
    external_weather_observed = not weather_obj.stale_or_missing_external_weather
    policy_risks: list[str] = []
    warnings: list[str] = []
    blocked: list[str] = []

    if weather_obj.rain_or_precipitation:
        policy_risks.append("precipitation_policy_risk")
        if external_weather_observed:
            blocked.append("source_weather_precipitation_observed")
    if weather_obj.source_unavailable:
        policy_risks.append("source_weather_unavailable")
        blocked.append("source_weather_unavailable")
    if weather_obj.wind_speed_mps is not None and weather_obj.wind_speed_mps >= 10:
        policy_risks.append("source_weather_wind_risk")
        warnings.append("source_weather_wind_margin_required")
    if route_obj.route_feasibility_status == "blocked_for_planning":
        policy_risks.append("route_feasibility_blocked")
        blocked.append("route_feasibility_blocked")
    if external_weather_required and not external_weather_observed:
        policy_risks.append("external_weather_snapshot_required")
        blocked.append("external_weather_snapshot_required_for_precipitation")
    elif weather_obj.stale_or_missing_external_weather:
        policy_risks.append("external_weather_snapshot_missing")
        warnings.append("external_weather_snapshot_missing")

    gate_status: Literal[
        "passed_for_planning",
        "warning_for_planning",
        "blocked_for_planning",
    ]
    if blocked:
        gate_status = "blocked_for_planning"
    elif warnings:
        gate_status = "warning_for_planning"
    else:
        gate_status = "passed_for_planning"
    operator_escalation_required = bool(blocked)

    hash_payload = {
        "weather_environment_snapshot_ref": weather_ref,
        "digital_twin_route_feasibility_ref": route_ref,
        "gate_status": gate_status,
        "operator_escalation_required": operator_escalation_required,
        "rain_or_precipitation": weather_obj.rain_or_precipitation,
        "external_weather_required": external_weather_required,
        "external_weather_observed": external_weather_observed,
        "max_precipitation_mm_per_hour": round(
            weather_obj.precipitation_mm_per_hour or 0.0,
            3,
        ),
        "min_visibility_m": 1500.0,
        "max_wind_speed_mps": round(weather_obj.wind_speed_mps or 0.0, 3),
        "policy_risk_labels": tuple(policy_risks),
        "warning_reasons": tuple(warnings),
        "blocked_reasons": tuple(blocked),
        "source_refs": (weather_ref, route_ref),
    }
    digest = _content_hash(hash_payload)
    gate_id_payload = {
        **hash_payload,
        "evaluated_at": evaluated_at.isoformat(),
        "gate_hash": digest,
    }
    return WeatherEnvironmentPolicyGate(
        gate_id=_stable_id("weather_environment_policy_gate", gate_id_payload),
        weather_environment_snapshot_ref=weather_ref,
        digital_twin_route_feasibility_ref=route_ref,
        source_refs=(weather_ref, route_ref),
        gate_status=gate_status,
        operator_escalation_required=operator_escalation_required,
        rain_or_precipitation=weather_obj.rain_or_precipitation,
        external_weather_required=external_weather_required,
        external_weather_observed=external_weather_observed,
        max_precipitation_mm_per_hour=round(
            weather_obj.precipitation_mm_per_hour or 0.0,
            3,
        ),
        min_visibility_m=1500.0,
        max_wind_speed_mps=round(weather_obj.wind_speed_mps or 0.0, 3),
        policy_risk_labels=tuple(policy_risks),
        warning_reasons=tuple(warnings),
        blocked_reasons=tuple(blocked),
        gate_hash=digest,
        sha256=digest,
        evaluated_at=evaluated_at,
    )


def build_vehicle_flight_envelope(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    route_feasibility: DigitalTwinRouteFeasibility | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    vehicle_profile_path: str | Path = DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
    vehicle_profile_root: Path = VEHICLE_PROFILE_ROOT_ABS,
    now: datetime | None = None,
) -> VehicleFlightEnvelope:
    target_obj = target if isinstance(target, RealWorldMissionTarget) else RealWorldMissionTarget.model_validate(target)
    route_obj = route_feasibility if isinstance(route_feasibility, DigitalTwinRouteFeasibility) else DigitalTwinRouteFeasibility.model_validate(route_feasibility)
    gate_obj = weather_policy_gate if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate) else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    evaluated_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    route_ref = digital_twin_route_feasibility_ref(route_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    if route_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError("vehicle envelope route target mismatch")
    if gate_obj.digital_twin_route_feasibility_ref != route_ref:
        raise DigitalTwinMissionEnvironmentError("vehicle envelope weather gate route mismatch")
    profile, profile_path, profile_sha256 = _load_vehicle_profile(
        vehicle_profile_path,
        profile_root=vehicle_profile_root,
    )
    vehicle_id = _clean_text(profile.get("vehicle_id"))
    profile_version = _clean_text(profile.get("profile_version"))
    if not vehicle_id or not profile_version:
        raise DigitalTwinMissionEnvironmentError("vehicle profile requires vehicle_id and profile_version")
    max_payload = float(profile.get("max_payload_kg", 0))
    max_range = float(profile.get("max_range_m", 0))
    max_altitude = float(profile.get("max_takeoff_altitude_m", 0))
    max_wind = float(profile.get("max_wind_speed_mps", 0))
    nominal_battery = float(profile.get("nominal_battery_wh", 0))
    reserve_percent = float(profile.get("reserve_percent", 0))
    cruise_wh_per_km = float(profile.get("cruise_energy_wh_per_km", 0))
    climb_wh_per_100m = float(profile.get("climb_energy_wh_per_100m", 0))
    requested_payload = float(target_obj.payload_weight_kg or 0.0)
    target_altitude = target_obj.requested_altitude_m or target_obj.altitude_m
    observed_wind = gate_obj.max_wind_speed_mps or None
    blocked: list[str] = []
    warnings: list[str] = []
    if requested_payload > max_payload:
        blocked.append("payload_over_limit")
    if route_obj.actual_route_distance_m > max_range:
        blocked.append("range_over_limit")
    if target_altitude is not None and float(target_altitude) > max_altitude:
        blocked.append("altitude_over_limit")
    if observed_wind is not None and observed_wind > max_wind:
        blocked.append("wind_over_limit")
    if gate_obj.gate_status == "blocked_for_planning":
        warnings.append("weather_policy_gate_blocked")
    status: Literal["passed", "blocked"] = "blocked" if blocked else "passed"
    source_refs = (target_ref, route_ref, gate_ref)
    profile_ref = f"vehicle_profile:{vehicle_id}:{profile_version}:{profile_sha256[:12]}"
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "digital_twin_route_feasibility_ref": route_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "source_refs": source_refs,
        "source_url": f"file://{profile_path}",
        "vehicle_profile_ref": profile_ref,
        "vehicle_profile_path": profile_path,
        "vehicle_profile_sha256": profile_sha256,
        "vehicle_id": vehicle_id,
        "profile_version": profile_version,
        "envelope_status": status,
        "max_payload_kg": max_payload,
        "requested_payload_kg": requested_payload,
        "max_range_m": max_range,
        "requested_route_distance_m": route_obj.actual_route_distance_m,
        "max_takeoff_altitude_m": max_altitude,
        "target_altitude_m": target_altitude,
        "max_wind_speed_mps": max_wind,
        "observed_wind_speed_mps": observed_wind,
        "nominal_battery_wh": nominal_battery,
        "reserve_percent": reserve_percent,
        "cruise_energy_wh_per_km": cruise_wh_per_km,
        "climb_energy_wh_per_100m": climb_wh_per_100m,
        "blocked_reasons": tuple(blocked),
        "warning_reasons": tuple(warnings),
    }
    digest = _content_hash(hash_payload)
    return VehicleFlightEnvelope(
        envelope_id=_stable_id("vehicle_flight_envelope", {**hash_payload, "evaluated_at": evaluated_at.isoformat(), "envelope_hash": digest}),
        real_world_mission_target_ref=target_ref,
        weather_environment_policy_gate_ref=gate_ref,
        source_refs=source_refs,
        source_url=f"file://{profile_path}",
        vehicle_profile_ref=profile_ref,
        vehicle_profile_path=profile_path,
        vehicle_profile_sha256=profile_sha256,
        vehicle_id=vehicle_id,
        profile_version=profile_version,
        envelope_status=status,
        max_payload_kg=max_payload,
        requested_payload_kg=requested_payload,
        max_range_m=max_range,
        requested_route_distance_m=route_obj.actual_route_distance_m,
        max_takeoff_altitude_m=max_altitude,
        target_altitude_m=target_altitude,
        max_wind_speed_mps=max_wind,
        observed_wind_speed_mps=observed_wind,
        nominal_battery_wh=nominal_battery,
        reserve_percent=reserve_percent,
        cruise_energy_wh_per_km=cruise_wh_per_km,
        climb_energy_wh_per_100m=climb_wh_per_100m,
        blocked_reasons=tuple(blocked),
        warning_reasons=tuple(warnings),
        envelope_hash=digest,
        sha256=digest,
        evaluated_at=evaluated_at,
    )


def build_mission_energy_budget(
    *,
    vehicle_envelope: VehicleFlightEnvelope | Mapping[str, Any],
    route_feasibility: DigitalTwinRouteFeasibility | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    now: datetime | None = None,
) -> MissionEnergyBudget:
    envelope_obj = vehicle_envelope if isinstance(vehicle_envelope, VehicleFlightEnvelope) else VehicleFlightEnvelope.model_validate(vehicle_envelope)
    route_obj = route_feasibility if isinstance(route_feasibility, DigitalTwinRouteFeasibility) else DigitalTwinRouteFeasibility.model_validate(route_feasibility)
    gate_obj = weather_policy_gate if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate) else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    computed_at = _utc(now)
    envelope_ref = vehicle_flight_envelope_ref(envelope_obj)
    route_ref = digital_twin_route_feasibility_ref(route_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    if envelope_obj.weather_environment_policy_gate_ref != gate_ref:
        raise DigitalTwinMissionEnvironmentError("energy budget weather gate mismatch")
    available = envelope_obj.nominal_battery_wh
    reserve = round(available * (envelope_obj.reserve_percent / 100.0), 3)
    cruise = round((route_obj.actual_route_distance_m / 1000.0) * envelope_obj.cruise_energy_wh_per_km, 3)
    climb = round((route_obj.elevation_gain_m / 100.0) * envelope_obj.climb_energy_wh_per_100m, 3)
    payload_margin = round(
        envelope_obj.requested_payload_kg * PAYLOAD_ENERGY_MARGIN_WH_PER_KG,
        3,
    )
    wind_margin = round(
        (gate_obj.max_wind_speed_mps or 0.0) * WIND_ENERGY_MARGIN_WH_PER_MPS,
        3,
    )
    required = round(reserve + cruise + climb + payload_margin + wind_margin, 3)
    remaining = round(available - required, 3)
    blocked = list(envelope_obj.blocked_reasons)
    warnings = list(envelope_obj.warning_reasons)
    if remaining < 0:
        blocked.append("insufficient_battery_energy")
    status: Literal["passed", "blocked"] = "blocked" if blocked else "passed"
    source_refs = (envelope_ref, route_ref, gate_ref)
    hash_payload = {
        "vehicle_flight_envelope_ref": envelope_ref,
        "digital_twin_route_feasibility_ref": route_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "source_refs": source_refs,
        "source_url": envelope_obj.source_url,
        "budget_status": status,
        "available_battery_wh": available,
        "reserve_battery_wh": reserve,
        "cruise_energy_wh": cruise,
        "climb_energy_wh": climb,
        "payload_energy_margin_wh": payload_margin,
        "wind_energy_margin_wh": wind_margin,
        "required_energy_wh": required,
        "remaining_energy_wh": remaining,
        "blocked_reasons": tuple(blocked),
        "warning_reasons": tuple(warnings),
    }
    digest = _content_hash(hash_payload)
    return MissionEnergyBudget(
        budget_id=_stable_id("mission_energy_budget", {**hash_payload, "computed_at": computed_at.isoformat(), "budget_hash": digest}),
        vehicle_flight_envelope_ref=envelope_ref,
        digital_twin_route_feasibility_ref=route_ref,
        weather_environment_policy_gate_ref=gate_ref,
        source_refs=source_refs,
        source_url=envelope_obj.source_url,
        budget_status=status,
        available_battery_wh=available,
        reserve_battery_wh=reserve,
        cruise_energy_wh=cruise,
        climb_energy_wh=climb,
        payload_energy_margin_wh=payload_margin,
        wind_energy_margin_wh=wind_margin,
        required_energy_wh=required,
        remaining_energy_wh=remaining,
        blocked_reasons=tuple(blocked),
        warning_reasons=tuple(warnings),
        budget_hash=digest,
        sha256=digest,
        computed_at=computed_at,
    )


def build_digital_twin_route_plan(
    *,
    target: RealWorldMissionTarget | Mapping[str, Any],
    terrain: TerrainEnvironmentSnapshot
    | TileBackedTerrainEnvironmentSnapshot
    | Mapping[str, Any],
    prompt_projected_terrain: TerrainEnvironmentSnapshot | Mapping[str, Any] | None = None,
    weather: WeatherEnvironmentSnapshot | Mapping[str, Any],
    route_feasibility: DigitalTwinRouteFeasibility | Mapping[str, Any],
    weather_policy_gate: WeatherEnvironmentPolicyGate | Mapping[str, Any],
    now: datetime | None = None,
) -> DigitalTwinRoutePlan:
    target_obj = (
        target
        if isinstance(target, RealWorldMissionTarget)
        else RealWorldMissionTarget.model_validate(target)
    )
    terrain_obj: TerrainEnvironmentSnapshot | TileBackedTerrainEnvironmentSnapshot
    if isinstance(terrain, (TerrainEnvironmentSnapshot, TileBackedTerrainEnvironmentSnapshot)):
        terrain_obj = terrain
    elif (
        isinstance(terrain, Mapping)
        and terrain.get("schema_version")
        == TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION
    ):
        terrain_obj = TileBackedTerrainEnvironmentSnapshot.model_validate(terrain)
    else:
        terrain_obj = TerrainEnvironmentSnapshot.model_validate(terrain)
    prompt_terrain_obj = (
        prompt_projected_terrain
        if isinstance(prompt_projected_terrain, TerrainEnvironmentSnapshot)
        else (
            TerrainEnvironmentSnapshot.model_validate(prompt_projected_terrain)
            if prompt_projected_terrain is not None
            else None
        )
    )
    weather_obj = (
        weather
        if isinstance(weather, WeatherEnvironmentSnapshot)
        else WeatherEnvironmentSnapshot.model_validate(weather)
    )
    route_obj = (
        route_feasibility
        if isinstance(route_feasibility, DigitalTwinRouteFeasibility)
        else DigitalTwinRouteFeasibility.model_validate(route_feasibility)
    )
    gate_obj = (
        weather_policy_gate
        if isinstance(weather_policy_gate, WeatherEnvironmentPolicyGate)
        else WeatherEnvironmentPolicyGate.model_validate(weather_policy_gate)
    )
    planned_at = _utc(now)
    target_ref = real_world_mission_target_ref(target_obj)
    terrain_ref = _terrain_input_ref(terrain_obj)
    is_tile_backed_input = isinstance(terrain_obj, TileBackedTerrainEnvironmentSnapshot)
    input_source: Literal["prompt_projected_terrain", "tile_backed_terrain"] = (
        "tile_backed_terrain" if is_tile_backed_input else "prompt_projected_terrain"
    )
    prompt_terrain_ref = (
        terrain_environment_snapshot_ref(prompt_terrain_obj)
        if prompt_terrain_obj is not None
        else terrain_ref
    )
    tile_terrain_ref = terrain_ref if is_tile_backed_input else ""
    weather_ref = weather_environment_snapshot_ref(weather_obj)
    route_ref = digital_twin_route_feasibility_ref(route_obj)
    gate_ref = weather_environment_policy_gate_ref(gate_obj)
    if terrain_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan terrain target mismatch"
        )
    if is_tile_backed_input:
        if prompt_terrain_obj is None:
            raise DigitalTwinMissionEnvironmentError(
                "tile-backed route plan requires prompt-projected terrain"
            )
        if prompt_terrain_obj.real_world_mission_target_ref != target_ref:
            raise DigitalTwinMissionEnvironmentError(
                "digital twin route plan prompt terrain target mismatch"
            )
    if weather_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan weather target mismatch"
        )
    if route_obj.real_world_mission_target_ref != target_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility target mismatch"
        )
    if route_obj.terrain_environment_snapshot_ref != terrain_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility terrain mismatch"
        )
    if route_obj.prompt_projected_terrain_environment_snapshot_ref != prompt_terrain_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility prompt terrain mismatch"
        )
    if route_obj.tile_backed_terrain_environment_snapshot_ref != tile_terrain_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility tile-backed terrain mismatch"
        )
    if route_obj.route_feasibility_input_source != input_source:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility terrain input source mismatch"
        )
    if route_obj.weather_environment_snapshot_ref != weather_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan feasibility weather mismatch"
        )
    if gate_obj.weather_environment_snapshot_ref != weather_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan gate weather mismatch"
        )
    if gate_obj.digital_twin_route_feasibility_ref != route_ref:
        raise DigitalTwinMissionEnvironmentError(
            "digital twin route plan gate feasibility mismatch"
        )

    warnings = list(route_obj.warning_reasons) + list(gate_obj.warning_reasons)
    blocked: list[str] = []
    status: Literal[
        "ready_for_planning",
        "warning_for_planning",
        "blocked_by_route_feasibility",
        "blocked_by_weather_policy_gate",
    ]
    if route_obj.route_feasibility_status == "blocked_for_planning":
        status = "blocked_by_route_feasibility"
        blocked.append("route_feasibility_blocked")
        blocked.extend(route_obj.blocked_reasons)
    elif gate_obj.gate_status == "blocked_for_planning":
        status = "blocked_by_weather_policy_gate"
        blocked.append("weather_policy_gate_blocked")
        blocked.extend(gate_obj.blocked_reasons)
    elif warnings:
        status = "warning_for_planning"
    else:
        status = "ready_for_planning"
    operator_escalation_required = bool(blocked) or (
        gate_obj.operator_escalation_required
    )

    route_risks = _text_tuple(
        (
            *route_obj.route_risk_labels,
            *gate_obj.policy_risk_labels,
        )
    )
    route_plan_mode: Literal[
        "stage1_projected_planning_route",
        "stage2_tile_backed_planning_route",
    ] = (
        "stage2_tile_backed_planning_route"
        if is_tile_backed_input
        else "stage1_projected_planning_route"
    )
    source_projection_kind: Literal[
        "prompt_fixture_projection",
        "fixture_tile_backed_terrain_projection",
    ] = (
        "fixture_tile_backed_terrain_projection"
        if is_tile_backed_input
        else "prompt_fixture_projection"
    )
    source_refs = _text_tuple(
        (
            target_ref,
            terrain_ref,
            prompt_terrain_ref,
            tile_terrain_ref,
            weather_ref,
            route_ref,
            gate_ref,
        )
    )
    hash_payload = {
        "real_world_mission_target_ref": target_ref,
        "terrain_environment_snapshot_ref": terrain_ref,
        "prompt_projected_terrain_environment_snapshot_ref": prompt_terrain_ref,
        "tile_backed_terrain_environment_snapshot_ref": tile_terrain_ref,
        "weather_environment_snapshot_ref": weather_ref,
        "digital_twin_route_feasibility_ref": route_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "route_plan_status": status,
        "route_plan_mode": route_plan_mode,
        "source_projection_kind": source_projection_kind,
        "route_feasibility_input_source": input_source,
        "route_feasibility_status": route_obj.route_feasibility_status,
        "weather_policy_gate_status": gate_obj.gate_status,
        "operator_escalation_required": operator_escalation_required,
        "requested_distance_m": route_obj.requested_distance_m,
        "planned_route_distance_m": route_obj.actual_route_distance_m,
        "elevation_gain_m": route_obj.elevation_gain_m,
        "average_slope_percent": route_obj.average_slope_percent,
        "max_projected_slope_percent": route_obj.max_projected_slope_percent,
        "terrain_clearance_min_m": route_obj.min_terrain_clearance_m,
        "payload_weight_kg": route_obj.payload_weight_kg,
        "rain_or_precipitation": weather_obj.rain_or_precipitation,
        "digital_twin_world_generated": False,
        "sitl_world_binding_status": "not_generated",
        "coordinate_transform_status": "not_generated",
        "px4_mission_items_generated": False,
        "route_risk_labels": route_risks,
        "warning_reasons": _text_tuple(warnings),
        "blocked_reasons": _text_tuple(blocked),
        "source_refs": source_refs,
    }
    digest = _content_hash(hash_payload)
    route_plan_id_payload = {
        **hash_payload,
        "planned_at": planned_at.isoformat(),
        "route_plan_hash": digest,
    }
    return DigitalTwinRoutePlan(
        route_plan_id=_stable_id("digital_twin_route_plan", route_plan_id_payload),
        real_world_mission_target_ref=target_ref,
        terrain_environment_snapshot_ref=terrain_ref,
        prompt_projected_terrain_environment_snapshot_ref=prompt_terrain_ref,
        tile_backed_terrain_environment_snapshot_ref=tile_terrain_ref,
        weather_environment_snapshot_ref=weather_ref,
        digital_twin_route_feasibility_ref=route_ref,
        weather_environment_policy_gate_ref=gate_ref,
        source_refs=source_refs,
        route_plan_status=status,
        route_plan_mode=route_plan_mode,
        source_projection_kind=source_projection_kind,
        route_feasibility_input_source=input_source,
        route_feasibility_status=route_obj.route_feasibility_status,
        weather_policy_gate_status=gate_obj.gate_status,
        operator_escalation_required=operator_escalation_required,
        requested_distance_m=route_obj.requested_distance_m,
        planned_route_distance_m=route_obj.actual_route_distance_m,
        elevation_gain_m=route_obj.elevation_gain_m,
        average_slope_percent=route_obj.average_slope_percent,
        max_projected_slope_percent=route_obj.max_projected_slope_percent,
        terrain_clearance_min_m=route_obj.min_terrain_clearance_m,
        payload_weight_kg=route_obj.payload_weight_kg,
        rain_or_precipitation=weather_obj.rain_or_precipitation,
        route_risk_labels=route_risks,
        warning_reasons=_text_tuple(warnings),
        blocked_reasons=_text_tuple(blocked),
        route_plan_hash=digest,
        sha256=digest,
        planned_at=planned_at,
    )


def build_digital_twin_stage1_environment(
    *,
    prompt: str,
    prompt_request_ref: str,
    altitude_target_m: int | float | None,
    payload_weight_kg: int | float | None,
    weather_hazard_labels: Sequence[str],
    now: datetime | None = None,
    source_backed_target_latitude: int | float | None = None,
    source_backed_target_longitude: int | float | None = None,
    source_backed_takeoff_latitude: int | float | None = None,
    source_backed_takeoff_longitude: int | float | None = None,
    source_backed_target_bbox: Sequence[float] | None = None,
    source_backed_dem_fetcher: Any | None = None,
    source_backed_weather_fetcher: Any | None = None,
    use_source_backed_weather: bool = False,
    vehicle_profile_path: str | Path | None = None,
    vehicle_profile_root: Path = VEHICLE_PROFILE_ROOT_ABS,
) -> dict[str, Any]:
    target = build_real_world_mission_target(
        prompt=prompt,
        prompt_request_ref=prompt_request_ref,
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
        now=now,
    )
    source_backed_requested = (
        source_backed_target_latitude is not None
        and source_backed_target_longitude is not None
    )
    target_resolution = None
    dem_source_snapshot = None
    dem_tile_request = None
    if source_backed_requested:
        target_resolution = build_real_world_target_resolution(
            target=target,
            latitude=float(source_backed_target_latitude),
            longitude=float(source_backed_target_longitude),
            altitude_m=altitude_target_m,
            bbox=source_backed_target_bbox,
            now=now,
        )
    geocode_candidate = build_real_world_geocode_candidate(
        target=target,
        target_resolution=target_resolution,
        now=now,
    )
    if source_backed_requested:
        dem_source_snapshot = build_terrain_dem_source_snapshot(
            target=target,
            target_resolution=target_resolution,
            now=now,
            fetcher=source_backed_dem_fetcher,
        )
        if dem_source_snapshot.snapshot_status == "source_backed_dem_captured":
            dem_tile_request = build_source_backed_terrain_dem_tile_request_candidate(
                target=target,
                geocode_candidate=geocode_candidate,
                target_resolution=target_resolution,
                dem_source_snapshot=dem_source_snapshot,
                now=now,
            )
    else:
        dem_tile_request = build_terrain_dem_tile_request_candidate(
            target=target,
            geocode_candidate=geocode_candidate,
            now=now,
        )
    dem_tile_snapshot = (
        (
            build_terrain_dem_tile_snapshot_from_source_snapshot(
                target=target,
                geocode_candidate=geocode_candidate,
                dem_tile_request=dem_tile_request,
                dem_source_snapshot=dem_source_snapshot,
                now=now,
            )
            if source_backed_requested and dem_source_snapshot
            else build_terrain_dem_tile_snapshot(
                target=target,
                geocode_candidate=geocode_candidate,
                dem_tile_request=dem_tile_request,
                now=now,
            )
        )
        if dem_tile_request
        and dem_tile_request.tile_request_status
        in {"prepared_fixture_tile_request", "prepared_source_backed_tile_request"}
        else None
    )
    tile_backed_terrain = (
        build_tile_backed_terrain_environment_snapshot(
            target=target,
            geocode_candidate=geocode_candidate,
            dem_tile_snapshot=dem_tile_snapshot,
            route_feasibility_binding_status="bound_to_route_feasibility",
            now=now,
        )
        if dem_tile_snapshot
        else None
    )
    heightmap_candidate = (
        build_terrain_heightmap_candidate(
            target=target,
            geocode_candidate=geocode_candidate,
            dem_tile_snapshot=dem_tile_snapshot,
            tile_backed_terrain=tile_backed_terrain,
            now=now,
        )
        if dem_tile_snapshot and tile_backed_terrain
        else None
    )
    heightmap_artifact = (
        build_terrain_heightmap_artifact(
            heightmap_candidate=heightmap_candidate,
            dem_tile_snapshot=dem_tile_snapshot,
            tile_backed_terrain=tile_backed_terrain,
            now=now,
        )
        if heightmap_candidate and dem_tile_snapshot and tile_backed_terrain
        else None
    )
    heightmap_file_artifact = (
        build_terrain_heightmap_file_artifact(
            heightmap_artifact=heightmap_artifact,
            heightmap_candidate=heightmap_candidate,
            now=now,
        )
        if heightmap_artifact and heightmap_candidate
        else None
    )
    terrain = build_terrain_environment_snapshot(target=target, now=now)
    weather_source_snapshot = None
    if use_source_backed_weather:
        if target_resolution is None:
            raise DigitalTwinMissionEnvironmentError(
                "source-backed weather requires source-backed target resolution"
            )
        weather_source_snapshot = build_weather_source_snapshot(
            target=target,
            target_resolution=target_resolution,
            now=now,
            fetcher=source_backed_weather_fetcher,
        )
        weather = build_weather_environment_snapshot_from_source_snapshot(
            target=target,
            weather_source_snapshot=weather_source_snapshot,
            now=now,
        )
    else:
        weather = build_weather_environment_snapshot(
            target=target,
            weather_hazard_labels=weather_hazard_labels,
            now=now,
        )
    route_feasibility = build_digital_twin_route_feasibility(
        target=target,
        terrain=tile_backed_terrain or terrain,
        prompt_projected_terrain=terrain if tile_backed_terrain else None,
        weather=weather,
        now=now,
    )
    weather_policy_gate = build_weather_environment_policy_gate(
        weather=weather,
        route_feasibility=route_feasibility,
        now=now,
    )
    vehicle_envelope = None
    mission_energy_budget = None
    if vehicle_profile_path is not None:
        vehicle_envelope = build_vehicle_flight_envelope(
            target=target,
            route_feasibility=route_feasibility,
            weather_policy_gate=weather_policy_gate,
            vehicle_profile_path=vehicle_profile_path,
            vehicle_profile_root=vehicle_profile_root,
            now=now,
        )
        mission_energy_budget = build_mission_energy_budget(
            vehicle_envelope=vehicle_envelope,
            route_feasibility=route_feasibility,
            weather_policy_gate=weather_policy_gate,
            now=now,
        )
    route_plan = build_digital_twin_route_plan(
        target=target,
        terrain=tile_backed_terrain or terrain,
        prompt_projected_terrain=terrain if tile_backed_terrain else None,
        weather=weather,
        route_feasibility=route_feasibility,
        weather_policy_gate=weather_policy_gate,
        now=now,
    )
    gazebo_world_candidate = (
        build_gazebo_world_candidate(
            heightmap_file_artifact=heightmap_file_artifact,
            heightmap_artifact=heightmap_artifact,
            heightmap_candidate=heightmap_candidate,
            route_plan=route_plan,
            weather_policy_gate=weather_policy_gate,
            now=now,
        )
        if heightmap_file_artifact and heightmap_artifact and heightmap_candidate
        else None
    )
    gazebo_world_artifact = (
        build_gazebo_world_artifact(
            gazebo_world_candidate=gazebo_world_candidate,
            heightmap_file_artifact=heightmap_file_artifact,
            route_plan=route_plan,
            weather_policy_gate=weather_policy_gate,
            now=now,
        )
        if gazebo_world_candidate and heightmap_file_artifact
        else None
    )
    coordinate_transform_candidate = (
        build_coordinate_transform_candidate(
            gazebo_world_artifact=gazebo_world_artifact,
            gazebo_world_candidate=gazebo_world_candidate,
            heightmap_file_artifact=heightmap_file_artifact,
            route_plan=route_plan,
            geocode_candidate=geocode_candidate,
            now=now,
        )
        if gazebo_world_artifact and gazebo_world_candidate and heightmap_file_artifact
        else None
    )
    mission_anchor_candidate = (
        build_digital_twin_mission_anchor_candidate(
            gazebo_world_artifact=gazebo_world_artifact,
            gazebo_world_candidate=gazebo_world_candidate,
            coordinate_transform_candidate=coordinate_transform_candidate,
            route_plan=route_plan,
            geocode_candidate=geocode_candidate,
            explicit_takeoff_latitude=source_backed_takeoff_latitude,
            explicit_takeoff_longitude=source_backed_takeoff_longitude,
            now=now,
        )
        if (
            gazebo_world_artifact
            and gazebo_world_candidate
            and coordinate_transform_candidate
        )
        else None
    )
    px4_mission_item_candidate = (
        build_digital_twin_px4_mission_item_candidate(
            mission_anchor_candidate=mission_anchor_candidate,
            coordinate_transform_candidate=coordinate_transform_candidate,
            gazebo_world_artifact=gazebo_world_artifact,
            gazebo_world_candidate=gazebo_world_candidate,
            heightmap_file_artifact=heightmap_file_artifact,
            route_plan=route_plan,
            weather_policy_gate=weather_policy_gate,
            geocode_candidate=geocode_candidate,
            vehicle_flight_envelope=vehicle_envelope,
            require_anchor_terrain_sample=(
                bool(
                    dem_source_snapshot
                    and dem_source_snapshot.source_backed_terrain
                    and mission_anchor_candidate
                    and mission_anchor_candidate.anchor_candidate_status
                    == "anchors_available_for_planning"
                )
            ),
            now=now,
        )
        if (
            coordinate_transform_candidate
            and gazebo_world_artifact
            and gazebo_world_candidate
            and heightmap_file_artifact
        )
        else None
    )
    sitl_binding_gate = (
        build_digital_twin_sitl_binding_gate(
            gazebo_world_artifact=gazebo_world_artifact,
            coordinate_transform_candidate=coordinate_transform_candidate,
            px4_mission_item_candidate=px4_mission_item_candidate,
            weather_policy_gate=weather_policy_gate,
            route_plan=route_plan,
            now=now,
        )
        if (
            gazebo_world_artifact
            and coordinate_transform_candidate
            and px4_mission_item_candidate
        )
        else None
    )
    return {
        "real_world_mission_target": target.model_dump(mode="json"),
        "real_world_geocode_candidate": geocode_candidate.model_dump(mode="json"),
        "real_world_target_resolution": (
            target_resolution.model_dump(mode="json") if target_resolution else None
        ),
        "terrain_dem_source_snapshot": (
            dem_source_snapshot.model_dump(mode="json") if dem_source_snapshot else None
        ),
        "terrain_dem_tile_request_candidate": (
            dem_tile_request.model_dump(mode="json") if dem_tile_request else None
        ),
        "terrain_dem_tile_snapshot": (
            dem_tile_snapshot.model_dump(mode="json") if dem_tile_snapshot else None
        ),
        "tile_backed_terrain_environment_snapshot": (
            tile_backed_terrain.model_dump(mode="json")
            if tile_backed_terrain
            else None
        ),
        "terrain_heightmap_candidate": (
            heightmap_candidate.model_dump(mode="json")
            if heightmap_candidate
            else None
        ),
        "terrain_heightmap_artifact": (
            heightmap_artifact.model_dump(mode="json") if heightmap_artifact else None
        ),
        "terrain_heightmap_file_artifact": (
            heightmap_file_artifact.model_dump(mode="json")
            if heightmap_file_artifact
            else None
        ),
        "gazebo_world_candidate": (
            gazebo_world_candidate.model_dump(mode="json")
            if gazebo_world_candidate
            else None
        ),
        "gazebo_world_artifact": (
            gazebo_world_artifact.model_dump(mode="json")
            if gazebo_world_artifact
            else None
        ),
        "coordinate_transform_candidate": (
            coordinate_transform_candidate.model_dump(mode="json")
            if coordinate_transform_candidate
            else None
        ),
        "digital_twin_mission_anchor_candidate": (
            mission_anchor_candidate.model_dump(mode="json")
            if mission_anchor_candidate
            else None
        ),
        "digital_twin_px4_mission_item_candidate": (
            px4_mission_item_candidate.model_dump(mode="json")
            if px4_mission_item_candidate
            else None
        ),
        "digital_twin_sitl_binding_gate": (
            sitl_binding_gate.model_dump(mode="json") if sitl_binding_gate else None
        ),
        "terrain_environment_snapshot": terrain.model_dump(mode="json"),
        "weather_environment_snapshot": weather.model_dump(mode="json"),
        "weather_source_snapshot": (
            weather_source_snapshot.model_dump(mode="json")
            if weather_source_snapshot
            else None
        ),
        "digital_twin_route_feasibility": route_feasibility.model_dump(mode="json"),
        "weather_environment_policy_gate": weather_policy_gate.model_dump(
            mode="json"
        ),
        "vehicle_flight_envelope": (
            vehicle_envelope.model_dump(mode="json") if vehicle_envelope else None
        ),
        "mission_energy_budget": (
            mission_energy_budget.model_dump(mode="json")
            if mission_energy_budget
            else None
        ),
        "digital_twin_route_plan": route_plan.model_dump(mode="json"),
        "summary": {
            "real_world_mission_target_ref": real_world_mission_target_ref(target),
            "real_world_geocode_candidate_ref": real_world_geocode_candidate_ref(
                geocode_candidate
            ),
            "terrain_dem_tile_request_candidate_ref": (
                terrain_dem_tile_request_candidate_ref(dem_tile_request)
                if dem_tile_request
                else ""
            ),
            "real_world_target_resolution_ref": (
                real_world_target_resolution_ref(target_resolution)
                if target_resolution
                else ""
            ),
            "terrain_dem_source_snapshot_ref": (
                terrain_dem_source_snapshot_ref(dem_source_snapshot)
                if dem_source_snapshot
                else ""
            ),
            "terrain_dem_tile_snapshot_ref": (
                terrain_dem_tile_snapshot_ref(dem_tile_snapshot)
                if dem_tile_snapshot
                else ""
            ),
            "tile_backed_terrain_environment_snapshot_ref": (
                tile_backed_terrain_environment_snapshot_ref(tile_backed_terrain)
                if tile_backed_terrain
                else ""
            ),
            "terrain_heightmap_candidate_ref": (
                terrain_heightmap_candidate_ref(heightmap_candidate)
                if heightmap_candidate
                else ""
            ),
            "terrain_heightmap_artifact_ref": (
                terrain_heightmap_artifact_ref(heightmap_artifact)
                if heightmap_artifact
                else ""
            ),
            "terrain_heightmap_file_artifact_ref": (
                terrain_heightmap_file_artifact_ref(heightmap_file_artifact)
                if heightmap_file_artifact
                else ""
            ),
            "gazebo_world_candidate_ref": (
                gazebo_world_candidate_ref(gazebo_world_candidate)
                if gazebo_world_candidate
                else ""
            ),
            "gazebo_world_artifact_ref": (
                gazebo_world_artifact_ref(gazebo_world_artifact)
                if gazebo_world_artifact
                else ""
            ),
            "coordinate_transform_candidate_ref": (
                coordinate_transform_candidate_ref(coordinate_transform_candidate)
                if coordinate_transform_candidate
                else ""
            ),
            "digital_twin_mission_anchor_candidate_ref": (
                digital_twin_mission_anchor_candidate_ref(mission_anchor_candidate)
                if mission_anchor_candidate
                else ""
            ),
            "digital_twin_px4_mission_item_candidate_ref": (
                digital_twin_px4_mission_item_candidate_ref(
                    px4_mission_item_candidate
                )
                if px4_mission_item_candidate
                else ""
            ),
            "digital_twin_sitl_binding_gate_ref": (
                digital_twin_sitl_binding_gate_ref(sitl_binding_gate)
                if sitl_binding_gate
                else ""
            ),
            "terrain_environment_snapshot_ref": terrain_environment_snapshot_ref(
                terrain
            ),
            "weather_environment_snapshot_ref": weather_environment_snapshot_ref(
                weather
            ),
            "weather_source_snapshot_ref": (
                weather_source_snapshot_ref(weather_source_snapshot)
                if weather_source_snapshot
                else ""
            ),
            "digital_twin_route_feasibility_ref": digital_twin_route_feasibility_ref(
                route_feasibility
            ),
            "weather_environment_policy_gate_ref": (
                weather_environment_policy_gate_ref(weather_policy_gate)
            ),
            "vehicle_flight_envelope_ref": (
                vehicle_flight_envelope_ref(vehicle_envelope)
                if vehicle_envelope
                else ""
            ),
            "mission_energy_budget_ref": (
                mission_energy_budget_ref(mission_energy_budget)
                if mission_energy_budget
                else ""
            ),
            "digital_twin_route_plan_ref": digital_twin_route_plan_ref(route_plan),
            "requested_distance_km": target.requested_distance_km,
            "target_resolution_status": target.target_resolution_status,
            "geocode_candidate_status": geocode_candidate.candidate_status,
            "geocode_candidate_mode": geocode_candidate.geocode_mode,
            "geocode_candidate_provider": geocode_candidate.provider,
            "geocode_candidate_source_url": geocode_candidate.source_url,
            "geocode_candidate_latitude": geocode_candidate.latitude,
            "geocode_candidate_longitude": geocode_candidate.longitude,
            "geocode_candidate_altitude_m": geocode_candidate.altitude_m,
            "digital_twin_stage2_target_resolution": (
                "source_backed_operator_confirmed"
                if target_resolution
                else "fixture_backed_planning_only"
            ),
            "source_backed_target": bool(target_resolution),
            "source_backed_terrain": (
                bool(dem_source_snapshot and dem_source_snapshot.source_backed_terrain)
            ),
            "source_unavailable": (
                bool(dem_source_snapshot and dem_source_snapshot.source_unavailable)
            ),
            "target_resolution_source_url": (
                target_resolution.source_url if target_resolution else ""
            ),
            "terrain_dem_source_snapshot_status": (
                dem_source_snapshot.snapshot_status if dem_source_snapshot else ""
            ),
            "terrain_dem_source_snapshot_provider": (
                dem_source_snapshot.provider if dem_source_snapshot else ""
            ),
            "terrain_dem_source_snapshot_source_url": (
                dem_source_snapshot.source_url if dem_source_snapshot else ""
            ),
            "terrain_dem_source_snapshot_provider_response_status": (
                dem_source_snapshot.provider_response_status
                if dem_source_snapshot
                else ""
            ),
            "dem_tile_request_status": (
                dem_tile_request.tile_request_status
                if dem_tile_request
                else "not_generated"
            ),
            "dem_tile_request_mode": (
                dem_tile_request.request_mode if dem_tile_request else ""
            ),
            "dem_tile_request_provider": (
                dem_tile_request.provider if dem_tile_request else ""
            ),
            "dem_tile_request_source_url": (
                dem_tile_request.source_url if dem_tile_request else ""
            ),
            "dem_tile_request_tile_refs": (
                list(dem_tile_request.tile_refs) if dem_tile_request else []
            ),
            "dem_tile_request_live_fetch_performed": (
                dem_tile_request.live_fetch_performed if dem_tile_request else False
            ),
            "dem_tile_request_terrain_snapshot_generated": (
                dem_tile_request.terrain_snapshot_generated
                if dem_tile_request
                else False
            ),
            "dem_tile_request_heightmap_generated": (
                dem_tile_request.heightmap_generated if dem_tile_request else False
            ),
            "dem_tile_snapshot_mode": (
                dem_tile_snapshot.snapshot_mode if dem_tile_snapshot else "not_generated"
            ),
            "dem_tile_snapshot_source_url": (
                dem_tile_snapshot.source_url if dem_tile_snapshot else ""
            ),
            "dem_tile_snapshot_tile_refs": (
                list(dem_tile_snapshot.tile_refs) if dem_tile_snapshot else []
            ),
            "dem_tile_snapshot_elevation_min_m": (
                dem_tile_snapshot.elevation_min_m if dem_tile_snapshot else None
            ),
            "dem_tile_snapshot_elevation_max_m": (
                dem_tile_snapshot.elevation_max_m if dem_tile_snapshot else None
            ),
            "dem_tile_snapshot_no_data_ratio": (
                dem_tile_snapshot.no_data_ratio if dem_tile_snapshot else None
            ),
            "dem_tile_snapshot_live_fetch_performed": (
                dem_tile_snapshot.live_fetch_performed if dem_tile_snapshot else False
            ),
            "dem_tile_snapshot_heightmap_generated": (
                dem_tile_snapshot.heightmap_generated if dem_tile_snapshot else False
            ),
            "tile_backed_terrain_snapshot_mode": (
                tile_backed_terrain.snapshot_mode
                if tile_backed_terrain
                else "not_generated"
            ),
            "tile_backed_terrain_route_binding_status": (
                tile_backed_terrain.route_feasibility_binding_status
                if tile_backed_terrain
                else "not_bound"
            ),
            "tile_backed_terrain_source_url": (
                tile_backed_terrain.source_url if tile_backed_terrain else ""
            ),
            "tile_backed_terrain_elevation_min_m": (
                tile_backed_terrain.elevation_min_m if tile_backed_terrain else None
            ),
            "tile_backed_terrain_elevation_max_m": (
                tile_backed_terrain.elevation_max_m if tile_backed_terrain else None
            ),
            "tile_backed_terrain_slope_risk_label": (
                tile_backed_terrain.slope_risk_label
                if tile_backed_terrain
                else "unknown"
            ),
            "tile_backed_terrain_heightmap_generated": (
                tile_backed_terrain.heightmap_generated
                if tile_backed_terrain
                else False
            ),
            "heightmap_candidate_status": (
                heightmap_candidate.heightmap_status
                if heightmap_candidate
                else "not_generated"
            ),
            "heightmap_candidate_mode": (
                heightmap_candidate.candidate_mode
                if heightmap_candidate
                else "not_generated"
            ),
            "heightmap_candidate_source_url": (
                heightmap_candidate.source_url if heightmap_candidate else ""
            ),
            "heightmap_candidate_pixel_width": (
                heightmap_candidate.pixel_width if heightmap_candidate else None
            ),
            "heightmap_candidate_pixel_height": (
                heightmap_candidate.pixel_height if heightmap_candidate else None
            ),
            "heightmap_candidate_vertical_scale_m": (
                heightmap_candidate.vertical_scale_m if heightmap_candidate else None
            ),
            "heightmap_candidate_artifact_materialized": (
                heightmap_candidate.artifact_materialized
                if heightmap_candidate
                else False
            ),
            "heightmap_candidate_gazebo_world_generated": (
                heightmap_candidate.gazebo_world_generated
                if heightmap_candidate
                else False
            ),
            "heightmap_artifact_status": (
                heightmap_artifact.artifact_status
                if heightmap_artifact
                else "not_generated"
            ),
            "heightmap_artifact_materialized": (
                heightmap_artifact.artifact_materialized
                if heightmap_artifact
                else False
            ),
            "heightmap_artifact_format": (
                heightmap_artifact.artifact_format if heightmap_artifact else ""
            ),
            "heightmap_artifact_encoding": (
                heightmap_artifact.encoding if heightmap_artifact else ""
            ),
            "heightmap_artifact_pixel_width": (
                heightmap_artifact.pixel_width if heightmap_artifact else None
            ),
            "heightmap_artifact_pixel_height": (
                heightmap_artifact.pixel_height if heightmap_artifact else None
            ),
            "heightmap_artifact_vertical_scale_m": (
                heightmap_artifact.vertical_scale_m if heightmap_artifact else None
            ),
            "heightmap_artifact_elevation_min_m": (
                heightmap_artifact.elevation_min_m if heightmap_artifact else None
            ),
            "heightmap_artifact_elevation_max_m": (
                heightmap_artifact.elevation_max_m if heightmap_artifact else None
            ),
            "heightmap_artifact_elevation_mean_m": (
                heightmap_artifact.elevation_mean_m if heightmap_artifact else None
            ),
            "heightmap_artifact_candidate_hash": (
                heightmap_artifact.candidate_hash if heightmap_artifact else ""
            ),
            "heightmap_artifact_sha256": (
                heightmap_artifact.artifact_sha256 if heightmap_artifact else ""
            ),
            "heightmap_artifact_gazebo_world_generated": (
                heightmap_artifact.gazebo_world_generated
                if heightmap_artifact
                else False
            ),
            "heightmap_artifact_coordinate_transform_generated": (
                heightmap_artifact.coordinate_transform_generated
                if heightmap_artifact
                else False
            ),
            "heightmap_artifact_px4_mission_items_generated": (
                heightmap_artifact.px4_mission_items_generated
                if heightmap_artifact
                else False
            ),
            "heightmap_file_artifact_status": (
                heightmap_file_artifact.file_artifact_status
                if heightmap_file_artifact
                else "not_generated"
            ),
            "heightmap_file_materialized": (
                heightmap_file_artifact.file_materialized
                if heightmap_file_artifact
                else False
            ),
            "heightmap_file_format": (
                heightmap_file_artifact.file_format
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_encoding": (
                heightmap_file_artifact.encoding
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_gazebo_dem_format": (
                heightmap_file_artifact.gazebo_dem_file_format
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_gazebo_dem_encoding": (
                heightmap_file_artifact.gazebo_dem_encoding
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_pixel_width": (
                heightmap_file_artifact.pixel_width
                if heightmap_file_artifact
                else None
            ),
            "heightmap_file_pixel_height": (
                heightmap_file_artifact.pixel_height
                if heightmap_file_artifact
                else None
            ),
            "heightmap_file_vertical_scale_m": (
                heightmap_file_artifact.vertical_scale_m
                if heightmap_file_artifact
                else None
            ),
            "heightmap_file_artifact_sha256": (
                heightmap_file_artifact.artifact_sha256
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_sha256": (
                heightmap_file_artifact.file_sha256 if heightmap_file_artifact else ""
            ),
            "heightmap_file_path_or_artifact_uri": (
                heightmap_file_artifact.file_path_or_artifact_uri
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_gazebo_dem_sha256": (
                heightmap_file_artifact.gazebo_dem_file_sha256
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_gazebo_dem_path_or_artifact_uri": (
                heightmap_file_artifact.gazebo_dem_file_path_or_artifact_uri
                if heightmap_file_artifact
                else ""
            ),
            "heightmap_file_gazebo_world_generated": (
                heightmap_file_artifact.gazebo_world_generated
                if heightmap_file_artifact
                else False
            ),
            "heightmap_file_coordinate_transform_generated": (
                heightmap_file_artifact.coordinate_transform_generated
                if heightmap_file_artifact
                else False
            ),
            "heightmap_file_px4_mission_items_generated": (
                heightmap_file_artifact.px4_mission_items_generated
                if heightmap_file_artifact
                else False
            ),
            "gazebo_world_candidate_status": (
                gazebo_world_candidate.world_candidate_status
                if gazebo_world_candidate
                else "not_generated"
            ),
            "gazebo_world_artifact_status": (
                gazebo_world_artifact.world_artifact_status
                if gazebo_world_artifact
                else "not_generated"
            ),
            "gazebo_world_format": (
                gazebo_world_artifact.world_format
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.world_format
                    if gazebo_world_candidate
                    else ""
                )
            ),
            "gazebo_world_file_path_or_artifact_uri": (
                gazebo_world_artifact.world_file_path_or_artifact_uri
                if gazebo_world_artifact
                else ""
            ),
            "gazebo_world_heightmap_uri": (
                gazebo_world_artifact.heightmap_uri
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.heightmap_uri
                    if gazebo_world_candidate
                    else ""
                )
            ),
            "gazebo_world_heightmap_file_sha256": (
                gazebo_world_artifact.heightmap_file_sha256
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.file_sha256
                    if gazebo_world_candidate
                    else ""
                )
            ),
            "gazebo_world_file_sha256": (
                gazebo_world_artifact.world_file_sha256
                if gazebo_world_artifact
                else ""
            ),
            "gazebo_world_terrain_scale": (
                list(gazebo_world_artifact.terrain_scale)
                if gazebo_world_artifact
                else (
                    list(gazebo_world_candidate.terrain_scale)
                    if gazebo_world_candidate
                    else []
                )
            ),
            "gazebo_world_vertical_scale_m": (
                gazebo_world_artifact.vertical_scale_m
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.vertical_scale_m
                    if gazebo_world_candidate
                    else None
                )
            ),
            "gazebo_world_bbox": (
                list(gazebo_world_artifact.bbox)
                if gazebo_world_artifact
                else (
                    list(gazebo_world_candidate.bbox)
                    if gazebo_world_candidate
                    else []
                )
            ),
            "gazebo_world_route_plan_status": (
                gazebo_world_artifact.route_plan_status
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.route_plan_status
                    if gazebo_world_candidate
                    else "not_generated"
                )
            ),
            "gazebo_world_weather_policy_gate_status": (
                gazebo_world_artifact.weather_policy_gate_status
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.weather_policy_gate_status
                    if gazebo_world_candidate
                    else "not_generated"
                )
            ),
            "gazebo_world_execution_binding_allowed": (
                gazebo_world_artifact.execution_binding_allowed
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.execution_binding_allowed
                    if gazebo_world_candidate
                    else False
                )
            ),
            "gazebo_world_materialized": (
                gazebo_world_artifact.gazebo_world_materialized
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.gazebo_world_materialized
                    if gazebo_world_candidate
                    else False
                )
            ),
            "gazebo_world_candidate_sha256": (
                gazebo_world_candidate.world_candidate_sha256
                if gazebo_world_candidate
                else ""
            ),
            "gazebo_world_artifact_sha256": (
                gazebo_world_artifact.sha256
                if gazebo_world_artifact
                else ""
            ),
            "gazebo_world_candidate_gazebo_execution_invoked": (
                gazebo_world_candidate.gazebo_execution_invoked
                if gazebo_world_candidate
                else False
            ),
            "gazebo_world_artifact_gazebo_execution_invoked": (
                gazebo_world_artifact.gazebo_execution_invoked
                if gazebo_world_artifact
                else False
            ),
            "gazebo_world_coordinate_transform_generated": (
                gazebo_world_artifact.coordinate_transform_generated
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.coordinate_transform_generated
                    if gazebo_world_candidate
                    else False
                )
            ),
            "gazebo_world_px4_mission_items_generated": (
                gazebo_world_artifact.px4_mission_items_generated
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.px4_mission_items_generated
                    if gazebo_world_candidate
                    else False
                )
            ),
            "gazebo_world_sitl_execution_bound": (
                gazebo_world_artifact.sitl_execution_bound
                if gazebo_world_artifact
                else (
                    gazebo_world_candidate.sitl_execution_bound
                    if gazebo_world_candidate
                    else False
                )
            ),
            "coordinate_transform_candidate_status": (
                coordinate_transform_candidate.transform_candidate_status
                if coordinate_transform_candidate
                else "not_generated"
            ),
            "coordinate_transform_frame_source": (
                coordinate_transform_candidate.coordinate_frame_source
                if coordinate_transform_candidate
                else ""
            ),
            "coordinate_transform_frame_target": (
                coordinate_transform_candidate.coordinate_frame_target
                if coordinate_transform_candidate
                else ""
            ),
            "coordinate_transform_origin_latitude": (
                coordinate_transform_candidate.origin_latitude
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_origin_longitude": (
                coordinate_transform_candidate.origin_longitude
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_origin_altitude_m": (
                coordinate_transform_candidate.origin_altitude_m
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_world_origin_x_m": (
                coordinate_transform_candidate.world_origin_x_m
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_world_origin_y_m": (
                coordinate_transform_candidate.world_origin_y_m
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_world_origin_z_m": (
                coordinate_transform_candidate.world_origin_z_m
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_meters_per_degree_lat": (
                coordinate_transform_candidate.meters_per_degree_lat
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_meters_per_degree_lon": (
                coordinate_transform_candidate.meters_per_degree_lon
                if coordinate_transform_candidate
                else None
            ),
            "coordinate_transform_terrain_scale": (
                list(coordinate_transform_candidate.terrain_scale)
                if coordinate_transform_candidate
                else []
            ),
            "coordinate_transform_bbox": (
                list(coordinate_transform_candidate.bbox)
                if coordinate_transform_candidate
                else []
            ),
            "coordinate_transform_hash": (
                coordinate_transform_candidate.transform_hash
                if coordinate_transform_candidate
                else ""
            ),
            "coordinate_transform_materialized": (
                coordinate_transform_candidate.coordinate_transform_materialized
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_execution_binding_allowed": (
                coordinate_transform_candidate.execution_binding_allowed
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_px4_mission_items_generated": (
                coordinate_transform_candidate.px4_mission_items_generated
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_sitl_execution_bound": (
                coordinate_transform_candidate.sitl_execution_bound
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_gazebo_execution_invoked": (
                coordinate_transform_candidate.gazebo_execution_invoked
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_hardware_target_allowed": (
                coordinate_transform_candidate.hardware_target_allowed
                if coordinate_transform_candidate
                else False
            ),
            "coordinate_transform_physical_execution_invoked": (
                coordinate_transform_candidate.physical_execution_invoked
                if coordinate_transform_candidate
                else False
            ),
            "mission_anchor_candidate_status": (
                mission_anchor_candidate.anchor_candidate_status
                if mission_anchor_candidate
                else "not_generated"
            ),
            "mission_anchor_candidate_mode": (
                mission_anchor_candidate.anchor_mode if mission_anchor_candidate else ""
            ),
            "mission_anchor_candidate_takeoff_anchor_ref": (
                mission_anchor_candidate.takeoff_anchor_ref
                if mission_anchor_candidate
                else ""
            ),
            "mission_anchor_candidate_takeoff_latitude_deg": (
                mission_anchor_candidate.takeoff_anchor_latitude_deg
                if mission_anchor_candidate
                else None
            ),
            "mission_anchor_candidate_takeoff_longitude_deg": (
                mission_anchor_candidate.takeoff_anchor_longitude_deg
                if mission_anchor_candidate
                else None
            ),
            "mission_anchor_candidate_takeoff_altitude_m_agl": (
                mission_anchor_candidate.takeoff_anchor_altitude_m_agl
                if mission_anchor_candidate
                else None
            ),
            "mission_anchor_candidate_dropoff_anchor_ref": (
                mission_anchor_candidate.dropoff_anchor_ref
                if mission_anchor_candidate
                else ""
            ),
            "mission_anchor_candidate_hash": (
                mission_anchor_candidate.anchor_hash if mission_anchor_candidate else ""
            ),
            "mission_anchor_candidate_blocked_reasons": (
                list(mission_anchor_candidate.blocked_reasons)
                if mission_anchor_candidate
                else []
            ),
            "mission_anchor_candidate_px4_mission_upload_allowed": (
                mission_anchor_candidate.px4_mission_upload_allowed
                if mission_anchor_candidate
                else False
            ),
            "mission_anchor_candidate_mavlink_dispatch_performed": (
                mission_anchor_candidate.mavlink_dispatch_performed
                if mission_anchor_candidate
                else False
            ),
            "mission_anchor_candidate_sitl_execution_bound": (
                mission_anchor_candidate.sitl_execution_bound
                if mission_anchor_candidate
                else False
            ),
            "mission_anchor_candidate_gazebo_execution_invoked": (
                mission_anchor_candidate.gazebo_execution_invoked
                if mission_anchor_candidate
                else False
            ),
            "mission_anchor_candidate_hardware_target_allowed": (
                mission_anchor_candidate.hardware_target_allowed
                if mission_anchor_candidate
                else False
            ),
            "mission_anchor_candidate_physical_execution_invoked": (
                mission_anchor_candidate.physical_execution_invoked
                if mission_anchor_candidate
                else False
            ),
            "px4_mission_item_candidate_status": (
                px4_mission_item_candidate.candidate_status
                if px4_mission_item_candidate
                else "not_generated"
            ),
            "px4_mission_item_candidate_mission_anchor_candidate_ref": (
                px4_mission_item_candidate.digital_twin_mission_anchor_candidate_ref
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_item_count": (
                px4_mission_item_candidate.candidate_item_count
                if px4_mission_item_candidate
                else 0
            ),
            "px4_mission_item_candidate_takeoff_anchor_ref": (
                px4_mission_item_candidate.takeoff_anchor_ref
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_takeoff_altitude_m": (
                px4_mission_item_candidate.candidate_items[0]["altitude_m"]
                if (
                    px4_mission_item_candidate
                    and px4_mission_item_candidate.candidate_items
                )
                else None
            ),
            "px4_mission_item_candidate_waypoint_altitude_m": (
                px4_mission_item_candidate.candidate_items[1]["altitude_m"]
                if (
                    px4_mission_item_candidate
                    and len(px4_mission_item_candidate.candidate_items) > 1
                )
                else None
            ),
            "px4_mission_item_candidate_takeoff_terrain_elevation_m": (
                px4_mission_item_candidate.takeoff_terrain_elevation_m
                if px4_mission_item_candidate
                else None
            ),
            "px4_mission_item_candidate_takeoff_agl_margin_m": (
                px4_mission_item_candidate.takeoff_agl_margin_m
                if px4_mission_item_candidate
                else None
            ),
            "px4_mission_item_candidate_terrain_sampling_mode": (
                px4_mission_item_candidate.terrain_sampling_mode
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_vehicle_flight_envelope_ref": (
                px4_mission_item_candidate.vehicle_flight_envelope_ref
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_vehicle_max_takeoff_altitude_m": (
                px4_mission_item_candidate.vehicle_max_takeoff_altitude_m
                if px4_mission_item_candidate
                else None
            ),
            "px4_mission_item_candidate_dropoff_target_ref": (
                px4_mission_item_candidate.dropoff_target_ref
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_route_plan_status": (
                px4_mission_item_candidate.route_plan_status
                if px4_mission_item_candidate
                else "not_generated"
            ),
            "px4_mission_item_candidate_weather_policy_gate_status": (
                px4_mission_item_candidate.weather_policy_gate_status
                if px4_mission_item_candidate
                else "not_generated"
            ),
            "px4_mission_item_candidate_coordinate_transform_materialized": (
                px4_mission_item_candidate.coordinate_transform_materialized
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_execution_binding_allowed": (
                px4_mission_item_candidate.execution_binding_allowed
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_px4_mission_upload_allowed": (
                px4_mission_item_candidate.px4_mission_upload_allowed
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_mavlink_dispatch_performed": (
                px4_mission_item_candidate.mavlink_dispatch_performed
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_sitl_execution_bound": (
                px4_mission_item_candidate.sitl_execution_bound
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_gazebo_execution_invoked": (
                px4_mission_item_candidate.gazebo_execution_invoked
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_hardware_target_allowed": (
                px4_mission_item_candidate.hardware_target_allowed
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_physical_execution_invoked": (
                px4_mission_item_candidate.physical_execution_invoked
                if px4_mission_item_candidate
                else False
            ),
            "px4_mission_item_candidate_hash": (
                px4_mission_item_candidate.mission_item_candidate_hash
                if px4_mission_item_candidate
                else ""
            ),
            "px4_mission_item_candidate_blocked_reasons": (
                list(px4_mission_item_candidate.blocked_reasons)
                if px4_mission_item_candidate
                else []
            ),
            "sitl_binding_gate_status": (
                sitl_binding_gate.binding_gate_status
                if sitl_binding_gate
                else "not_generated"
            ),
            "sitl_binding_gate_binding_allowed": (
                sitl_binding_gate.binding_allowed if sitl_binding_gate else False
            ),
            "sitl_binding_gate_binding_eligible": (
                sitl_binding_gate.binding_eligible if sitl_binding_gate else False
            ),
            "sitl_binding_gate_binding_mode": (
                sitl_binding_gate.binding_mode if sitl_binding_gate else ""
            ),
            "sitl_binding_gate_operator_approval_required": (
                sitl_binding_gate.operator_approval_required
                if sitl_binding_gate
                else True
            ),
            "sitl_binding_gate_server_opt_in_required": (
                sitl_binding_gate.server_opt_in_required
                if sitl_binding_gate
                else True
            ),
            "sitl_binding_gate_observed_facts_only": (
                sitl_binding_gate.observed_facts_only if sitl_binding_gate else True
            ),
            "sitl_binding_gate_route_plan_status": (
                sitl_binding_gate.route_plan_status
                if sitl_binding_gate
                else "not_generated"
            ),
            "sitl_binding_gate_weather_policy_gate_status": (
                sitl_binding_gate.weather_policy_gate_status
                if sitl_binding_gate
                else "not_generated"
            ),
            "sitl_binding_gate_px4_mission_item_candidate_status": (
                sitl_binding_gate.px4_mission_item_candidate_status
                if sitl_binding_gate
                else "not_generated"
            ),
            "sitl_binding_gate_candidate_item_count": (
                sitl_binding_gate.candidate_item_count if sitl_binding_gate else 0
            ),
            "sitl_binding_gate_coordinate_transform_materialized": (
                sitl_binding_gate.coordinate_transform_materialized
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_px4_mission_upload_allowed": (
                sitl_binding_gate.px4_mission_upload_allowed
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_mavlink_dispatch_performed": (
                sitl_binding_gate.mavlink_dispatch_performed
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_sitl_execution_bound": (
                sitl_binding_gate.sitl_execution_bound if sitl_binding_gate else False
            ),
            "sitl_binding_gate_gazebo_execution_invoked": (
                sitl_binding_gate.gazebo_execution_invoked
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_hardware_target_allowed": (
                sitl_binding_gate.hardware_target_allowed
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_physical_execution_invoked": (
                sitl_binding_gate.physical_execution_invoked
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_approval_free_stronger_execution_allowed": (
                sitl_binding_gate.approval_free_stronger_execution_allowed
                if sitl_binding_gate
                else False
            ),
            "sitl_binding_gate_hash": (
                sitl_binding_gate.binding_gate_hash if sitl_binding_gate else ""
            ),
            "sitl_binding_gate_blocked_reasons": (
                list(sitl_binding_gate.blocked_reasons)
                if sitl_binding_gate
                else []
            ),
            "terrain_snapshot_mode": terrain.snapshot_mode,
            "terrain_elevation_min_m": terrain.elevation_min_m,
            "terrain_elevation_max_m": terrain.elevation_max_m,
            "terrain_slope_risk_label": terrain.slope_risk_label,
            "weather_precipitation_label": weather.precipitation_label,
            "weather_source_snapshot_status": (
                weather_source_snapshot.snapshot_status
                if weather_source_snapshot
                else ""
            ),
            "weather_source_snapshot_provider": (
                weather_source_snapshot.provider if weather_source_snapshot else ""
            ),
            "weather_source_snapshot_source_url": (
                weather_source_snapshot.source_url if weather_source_snapshot else ""
            ),
            "weather_source_snapshot_provider_response_status": (
                weather_source_snapshot.provider_response_status
                if weather_source_snapshot
                else ""
            ),
            "source_backed_weather": (
                bool(
                    weather_source_snapshot
                    and weather_source_snapshot.source_backed_weather
                )
            ),
            "source_weather_unavailable": (
                bool(
                    weather_source_snapshot
                    and weather_source_snapshot.source_unavailable
                )
            ),
            "weather_precipitation_mm_per_hour": (
                weather.precipitation_mm_per_hour
            ),
            "weather_wind_speed_mps": weather.wind_speed_mps,
            "weather_external_snapshot_missing": (
                weather.stale_or_missing_external_weather
            ),
            "route_feasibility_status": route_feasibility.route_feasibility_status,
            "route_feasibility_input_source": (
                route_feasibility.route_feasibility_input_source
            ),
            "route_feasibility_terrain_environment_snapshot_ref": (
                route_feasibility.terrain_environment_snapshot_ref
            ),
            "route_feasibility_prompt_projected_terrain_environment_snapshot_ref": (
                route_feasibility.prompt_projected_terrain_environment_snapshot_ref
            ),
            "route_feasibility_tile_backed_terrain_environment_snapshot_ref": (
                route_feasibility.tile_backed_terrain_environment_snapshot_ref
            ),
            "route_actual_distance_m": route_feasibility.actual_route_distance_m,
            "route_elevation_gain_m": route_feasibility.elevation_gain_m,
            "route_average_slope_percent": route_feasibility.average_slope_percent,
            "route_max_projected_slope_percent": (
                route_feasibility.max_projected_slope_percent
            ),
            "route_risk_labels": list(route_feasibility.route_risk_labels),
            "route_warning_reasons": list(route_feasibility.warning_reasons),
            "route_blocked_reasons": list(route_feasibility.blocked_reasons),
            "weather_policy_gate_status": weather_policy_gate.gate_status,
            "weather_operator_escalation_required": (
                weather_policy_gate.operator_escalation_required
            ),
            "weather_external_weather_required": (
                weather_policy_gate.external_weather_required
            ),
            "weather_external_weather_observed": (
                weather_policy_gate.external_weather_observed
            ),
            "weather_policy_risk_labels": list(weather_policy_gate.policy_risk_labels),
            "weather_policy_warning_reasons": list(
                weather_policy_gate.warning_reasons
            ),
            "weather_policy_blocked_reasons": list(weather_policy_gate.blocked_reasons),
            "vehicle_envelope_status": (
                vehicle_envelope.envelope_status if vehicle_envelope else ""
            ),
            "vehicle_envelope_blocked_reasons": (
                list(vehicle_envelope.blocked_reasons) if vehicle_envelope else []
            ),
            "vehicle_profile_ref": (
                vehicle_envelope.vehicle_profile_ref if vehicle_envelope else ""
            ),
            "vehicle_hardware_target_allowed": (
                vehicle_envelope.hardware_target_allowed if vehicle_envelope else False
            ),
            "mission_energy_budget_status": (
                mission_energy_budget.budget_status if mission_energy_budget else ""
            ),
            "mission_energy_required_wh": (
                mission_energy_budget.required_energy_wh
                if mission_energy_budget
                else None
            ),
            "mission_energy_remaining_wh": (
                mission_energy_budget.remaining_energy_wh
                if mission_energy_budget
                else None
            ),
            "mission_energy_blocked_reasons": (
                list(mission_energy_budget.blocked_reasons)
                if mission_energy_budget
                else []
            ),
            "route_plan_status": route_plan.route_plan_status,
            "route_plan_mode": route_plan.route_plan_mode,
            "route_plan_source_projection_kind": route_plan.source_projection_kind,
            "route_plan_terrain_environment_snapshot_ref": (
                route_plan.terrain_environment_snapshot_ref
            ),
            "route_plan_prompt_projected_terrain_environment_snapshot_ref": (
                route_plan.prompt_projected_terrain_environment_snapshot_ref
            ),
            "route_plan_tile_backed_terrain_environment_snapshot_ref": (
                route_plan.tile_backed_terrain_environment_snapshot_ref
            ),
            "route_plan_blocked_reasons": list(route_plan.blocked_reasons),
            "route_plan_warning_reasons": list(route_plan.warning_reasons),
            "route_plan_operator_escalation_required": (
                route_plan.operator_escalation_required
            ),
            "route_plan_sitl_world_binding_status": (
                route_plan.sitl_world_binding_status
            ),
            "route_plan_coordinate_transform_status": (
                route_plan.coordinate_transform_status
            ),
            "digital_twin_stage": "stage2_planning_only",
            "digital_twin_stage_detail": (
                (
                    "sitl_binding_gate_blocked"
                    if sitl_binding_gate.binding_gate_status == "blocked"
                    else "sitl_binding_gate_eligible"
                )
                if sitl_binding_gate
                else (
                    (
                        "px4_mission_item_candidate_blocked"
                        if px4_mission_item_candidate.candidate_status.startswith(
                            "blocked_"
                        )
                        else "px4_mission_item_candidate_generated"
                    )
                    if px4_mission_item_candidate
                    else (
                        "coordinate_transform_candidate_generated"
                        if coordinate_transform_candidate
                        else (
                            "gazebo_world_artifact_materialized"
                            if gazebo_world_artifact
                            else (
                                "gazebo_world_candidate_generated"
                                if gazebo_world_candidate
                                else (
                                    "heightmap_file_artifact_materialized"
                                    if heightmap_file_artifact
                                    else (
                                        "heightmap_artifact_materialized"
                                        if heightmap_artifact
                                        else (
                                            "heightmap_candidate_generated"
                                            if heightmap_candidate
                                            else "dem_tile_request_prepared"
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            ),
            "digital_twin_world_generated": bool(gazebo_world_artifact),
        },
    }


def build_digital_twin_stage1_epic_exit_result(
    *,
    mission_designer_result: Mapping[str, Any],
    completed_at: datetime | None = None,
) -> DigitalTwinStage1EpicExitResult:
    completed = _utc(completed_at)
    prompt_request = dict(mission_designer_result.get("prompt_request") or {})
    target = RealWorldMissionTarget.model_validate(
        mission_designer_result.get("real_world_mission_target")
    )
    terrain = TerrainEnvironmentSnapshot.model_validate(
        mission_designer_result.get("terrain_environment_snapshot")
    )
    weather = WeatherEnvironmentSnapshot.model_validate(
        mission_designer_result.get("weather_environment_snapshot")
    )
    route = DigitalTwinRouteFeasibility.model_validate(
        mission_designer_result.get("digital_twin_route_feasibility")
    )
    gate = WeatherEnvironmentPolicyGate.model_validate(
        mission_designer_result.get("weather_environment_policy_gate")
    )
    plan = DigitalTwinRoutePlan.model_validate(
        mission_designer_result.get("digital_twin_route_plan")
    )
    target_ref = real_world_mission_target_ref(target)
    terrain_ref = terrain_environment_snapshot_ref(terrain)
    weather_ref = weather_environment_snapshot_ref(weather)
    route_ref = digital_twin_route_feasibility_ref(route)
    gate_ref = weather_environment_policy_gate_ref(gate)
    plan_ref = digital_twin_route_plan_ref(plan)
    prompt = _clean_text(prompt_request.get("prompt") or target.prompt_target)

    if target.requested_distance_km != 10.0:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires 10km distance"
        )
    if target.requested_altitude_m != 3000.0:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires 3000m altitude"
        )
    if target.payload_weight_kg != 3.0:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires 3kg payload"
        )
    if not weather.rain_or_precipitation:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires rain constraint"
        )
    if gate.gate_status != "blocked_for_planning":
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires blocked weather policy gate"
        )
    if plan.route_plan_status != "blocked_by_weather_policy_gate":
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires weather-blocked route plan"
        )
    if not plan.operator_escalation_required:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit requires operator escalation"
        )
    if plan.digital_twin_world_generated:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit forbids generated digital twin world"
        )
    if plan.px4_mission_items_generated:
        raise DigitalTwinMissionEnvironmentError(
            "stage1 epic exit forbids PX4 mission item generation"
        )

    blocked_reasons = _text_tuple(plan.blocked_reasons)
    warning_reasons = _text_tuple((*route.warning_reasons, *plan.warning_reasons))
    source_refs = (target_ref, terrain_ref, weather_ref, route_ref, gate_ref, plan_ref)
    hash_payload = {
        "prompt": prompt,
        "real_world_mission_target_ref": target_ref,
        "terrain_environment_snapshot_ref": terrain_ref,
        "weather_environment_snapshot_ref": weather_ref,
        "digital_twin_route_feasibility_ref": route_ref,
        "weather_environment_policy_gate_ref": gate_ref,
        "digital_twin_route_plan_ref": plan_ref,
        "source_refs": source_refs,
        "requested_distance_km": target.requested_distance_km,
        "requested_altitude_m": target.requested_altitude_m,
        "payload_weight_kg": target.payload_weight_kg,
        "rain_or_precipitation": weather.rain_or_precipitation,
        "route_feasibility_status": route.route_feasibility_status,
        "weather_policy_gate_status": gate.gate_status,
        "route_plan_status": plan.route_plan_status,
        "operator_escalation_required": plan.operator_escalation_required,
        "external_weather_required": gate.external_weather_required,
        "external_weather_observed": gate.external_weather_observed,
        "digital_twin_world_generated": plan.digital_twin_world_generated,
        "sitl_world_binding_status": plan.sitl_world_binding_status,
        "coordinate_transform_status": plan.coordinate_transform_status,
        "px4_mission_items_generated": plan.px4_mission_items_generated,
        "gazebo_execution_invoked": plan.gazebo_execution_invoked,
        "px4_mission_upload_allowed": plan.px4_mission_upload_allowed,
        "mavlink_dispatch_allowed": plan.mavlink_dispatch_allowed,
        "ros_dispatch_allowed": plan.ros_dispatch_allowed,
        "gazebo_entity_mutation_allowed": plan.gazebo_entity_mutation_allowed,
        "hardware_target_allowed": plan.hardware_target_allowed,
        "physical_execution_invoked": plan.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            plan.approval_free_stronger_execution_allowed
        ),
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
    }
    digest = _content_hash(hash_payload)
    result_id_payload = {
        **hash_payload,
        "completed_at": completed.isoformat(),
        "epic_exit_hash": digest,
    }
    return DigitalTwinStage1EpicExitResult(
        result_id=_stable_id("digital_twin_stage1_epic_exit", result_id_payload),
        prompt=prompt,
        real_world_mission_target_ref=target_ref,
        terrain_environment_snapshot_ref=terrain_ref,
        weather_environment_snapshot_ref=weather_ref,
        digital_twin_route_feasibility_ref=route_ref,
        weather_environment_policy_gate_ref=gate_ref,
        digital_twin_route_plan_ref=plan_ref,
        source_refs=source_refs,
        requested_distance_km=target.requested_distance_km,
        requested_altitude_m=target.requested_altitude_m,
        payload_weight_kg=target.payload_weight_kg,
        route_feasibility_status=route.route_feasibility_status,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        epic_exit_hash=digest,
        sha256=digest,
        completed_at=completed,
    )


__all__ = [
    "DigitalTwinMissionEnvironmentError",
    "DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION",
    "DIGITAL_TWIN_MISSION_ANCHOR_CANDIDATE_SCHEMA_VERSION",
    "DIGITAL_TWIN_PX4_MISSION_ITEM_CANDIDATE_SCHEMA_VERSION",
    "DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION",
    "DIGITAL_TWIN_STAGE1_EPIC_EXIT_SCHEMA_VERSION",
    "GAZEBO_WORLD_ARTIFACT_SCHEMA_VERSION",
    "GAZEBO_WORLD_CANDIDATE_SCHEMA_VERSION",
    "COORDINATE_TRANSFORM_CANDIDATE_SCHEMA_VERSION",
    "REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION",
    "REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION",
    "REAL_WORLD_TARGET_RESOLUTION_SCHEMA_VERSION",
    "SOURCE_BACKED_GSI_DEM_SOURCE_URL_PREFIX",
    "SOURCE_BACKED_GSI_DEM_TILE_URL_TEMPLATE",
    "SOURCE_BACKED_TARGET_RESOLUTION_SOURCE_URL",
    "STAGE1_TARGET_SOURCE_URL",
    "STAGE1_TERRAIN_SOURCE_URL",
    "STAGE1_WEATHER_SOURCE_URL",
    "TAKEOFF_FROM_TARGET_BEARING_DEG",
    "STAGE2_GEOCODE_SOURCE_URL",
    "STAGE2_DEM_TILE_INDEX_SOURCE_URL",
    "STAGE2_DEM_TILE_SNAPSHOT_SOURCE_URL",
    "STAGE2_GAZEBO_WORLD_ARTIFACT_SOURCE_URL",
    "STAGE2_GAZEBO_WORLD_CANDIDATE_SOURCE_URL",
    "STAGE2_COORDINATE_TRANSFORM_CANDIDATE_SOURCE_URL",
    "STAGE2_MISSION_ANCHOR_CANDIDATE_SOURCE_URL",
    "STAGE2_PX4_MISSION_ITEM_CANDIDATE_SOURCE_URL",
    "STAGE2_SITL_BINDING_GATE_SOURCE_URL",
    "STAGE2_HEIGHTMAP_ARTIFACT_SOURCE_URL",
    "STAGE2_HEIGHTMAP_CANDIDATE_SOURCE_URL",
    "STAGE2_HEIGHTMAP_FILE_ARTIFACT_SOURCE_URL",
    "STAGE2_TILE_BACKED_TERRAIN_SOURCE_URL",
    "TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION",
    "TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION",
    "TERRAIN_DEM_SOURCE_SNAPSHOT_SCHEMA_VERSION",
    "TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "TERRAIN_HEIGHTMAP_CANDIDATE_SCHEMA_VERSION",
    "TERRAIN_HEIGHTMAP_FILE_ARTIFACT_SCHEMA_VERSION",
    "TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION",
    "WEATHER_SOURCE_SNAPSHOT_SCHEMA_VERSION",
    "VEHICLE_FLIGHT_ENVELOPE_SCHEMA_VERSION",
    "MISSION_ENERGY_BUDGET_SCHEMA_VERSION",
    "DigitalTwinMissionAnchorCandidate",
    "DigitalTwinRouteFeasibility",
    "DigitalTwinPx4MissionItemCandidate",
    "DigitalTwinRoutePlan",
    "DigitalTwinSITLBindingGate",
    "DigitalTwinStage1EpicExitResult",
    "RealWorldGeocodeCandidate",
    "RealWorldMissionTarget",
    "RealWorldTargetResolution",
    "TerrainDemSourceSnapshot",
    "TerrainDemTileRequestCandidate",
    "TerrainDemTileSnapshot",
    "TerrainEnvironmentSnapshot",
    "TerrainHeightmapArtifact",
    "TerrainHeightmapCandidate",
    "TerrainHeightmapFileArtifact",
    "TileBackedTerrainEnvironmentSnapshot",
    "WeatherEnvironmentPolicyGate",
    "WeatherEnvironmentSnapshot",
    "WeatherSourceSnapshot",
    "VehicleFlightEnvelope",
    "MissionEnergyBudget",
    "CoordinateTransformCandidate",
    "GazeboWorldArtifact",
    "GazeboWorldCandidate",
    "build_coordinate_transform_candidate",
    "build_digital_twin_mission_anchor_candidate",
    "build_digital_twin_px4_mission_item_candidate",
    "build_digital_twin_sitl_binding_gate",
    "build_gazebo_world_artifact",
    "build_gazebo_world_candidate",
    "build_digital_twin_route_feasibility",
    "build_digital_twin_route_plan",
    "build_digital_twin_stage1_epic_exit_result",
    "build_digital_twin_stage1_environment",
    "build_real_world_geocode_candidate",
    "build_real_world_mission_target",
    "build_real_world_target_resolution",
    "build_source_backed_terrain_dem_tile_request_candidate",
    "build_terrain_dem_source_snapshot",
    "build_terrain_dem_tile_snapshot_from_source_snapshot",
    "build_terrain_dem_tile_request_candidate",
    "build_terrain_dem_tile_snapshot",
    "build_terrain_environment_snapshot",
    "build_terrain_heightmap_artifact",
    "build_terrain_heightmap_candidate",
    "build_terrain_heightmap_file_artifact",
    "build_tile_backed_terrain_environment_snapshot",
    "build_weather_environment_policy_gate",
    "build_weather_environment_snapshot",
    "build_weather_environment_snapshot_from_source_snapshot",
    "build_weather_source_snapshot",
    "build_vehicle_flight_envelope",
    "build_mission_energy_budget",
    "coordinate_transform_candidate_ref",
    "digital_twin_mission_anchor_candidate_ref",
    "digital_twin_px4_mission_item_candidate_ref",
    "digital_twin_sitl_binding_gate_ref",
    "digital_twin_route_feasibility_ref",
    "digital_twin_route_plan_ref",
    "digital_twin_stage1_epic_exit_ref",
    "gazebo_world_artifact_ref",
    "gazebo_world_candidate_ref",
    "real_world_geocode_candidate_ref",
    "real_world_mission_target_ref",
    "real_world_target_resolution_ref",
    "terrain_dem_source_snapshot_ref",
    "terrain_dem_tile_request_candidate_ref",
    "terrain_dem_tile_snapshot_ref",
    "terrain_environment_snapshot_ref",
    "terrain_heightmap_artifact_ref",
    "terrain_heightmap_candidate_ref",
    "terrain_heightmap_file_artifact_ref",
    "tile_backed_terrain_environment_snapshot_ref",
    "weather_environment_policy_gate_ref",
    "weather_environment_snapshot_ref",
    "weather_source_snapshot_ref",
    "vehicle_flight_envelope_ref",
    "mission_energy_budget_ref",
]
