"""Mission Designer source tools coordinated behind the MissionOS Chief.

The operator-facing contract stays at the MissionOS Chief boundary. Mission
Designer may use these internet-backed source tools to resolve coordinates and
weather before it builds a bounded proposal. The tools do not expose a
sub-agent conversation, grant authority, dispatch, or count progress.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION = "missionos_chief_planner_internal_tools.v1"
ROUTE_RESOLVER_TOOL_SCHEMA_VERSION = "missionos_route_resolver_tool_result.v1"
POSTAL_CODE_RESOLVER_TOOL_SCHEMA_VERSION = "missionos_postal_code_resolver_tool_result.v1"
WEATHER_RESOLVER_TOOL_SCHEMA_VERSION = "missionos_weather_resolver_tool_result.v1"
TERRAIN_ELEVATION_RESOLVER_TOOL_SCHEMA_VERSION = (
    "missionos_terrain_elevation_resolver_tool_result.v1"
)
SEMANTIC_ROUTE_REQUEST_SCHEMA_VERSION = "missionos_chief_semantic_route_request.v1"
CHIEF_ROUTE_FUNCTION_TOOL_SCHEMA_VERSION = (
    "missionos_chief_route_function_tool_invocation.v1"
)
CHIEF_ROUTE_FUNCTION_TOOL_NAME = "missionos_resolve_mission_designer_route"
PLACE_GEOCODER_TOOL_SCHEMA_VERSION = "missionos_place_geocoder_tool_result.v1"
COORDINATE_ROUTE_TOOL_SCHEMA_VERSION = "missionos_chief_coordinate_route.v1"
ZIPCLOUD_SEARCH_URL = "https://zipcloud.ibsnet.co.jp/api/search"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_JMA_URL_PREFIX = "https://api.open-meteo.com/v1/jma"
OPEN_METEO_FORECAST_URL_PREFIX = "https://api.open-meteo.com/v1/forecast"
GSI_DEM_TILE_URL_PREFIX = "https://cyberjapandata.gsi.go.jp/xyz/dem"
GSI_DEM_TILE_ZOOM = 14
SOURCE_TOOL_USER_AGENT = "boiled-claw-missionos-designer-source-tools/1.0"
OPEN_METEO_USER_AGENT = SOURCE_TOOL_USER_AGENT
GSI_DEM_USER_AGENT = SOURCE_TOOL_USER_AGENT
CHIEF_ROUTE_SEMANTIC_ADK_ENABLED_ENV = "MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED"
CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS_ENV = (
    "MISSIONOS_CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS"
)
DEFAULT_ORIGIN_QUERY = "東京駅"
DEFAULT_TERRAIN_CLEARANCE_AGL_M = 30.0
DEFAULT_TERRAIN_PROFILE_SAMPLE_COUNT = 5
_FULLWIDTH_NUMBER_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、",
    "0123456789.,,",
)
_WIND_SPEED_PATTERNS = (
    re.compile(r"風(?:速)?\s*(?:を)?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:m/s|メートル|mps|ｍ|m)", re.IGNORECASE),
    re.compile(r"風(?:速)?\s*(?:を)?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:キロ|ｋｍ|km)(?!\s*/?\s*h)", re.IGNORECASE),
    re.compile(r"wind\s*(?:speed)?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:m/s|mps|meters?)", re.IGNORECASE),
)
_WIND_DIRECTION_ALIASES = {
    "北": 0.0,
    "北風": 0.0,
    "north": 0.0,
    "n": 0.0,
    "東": 90.0,
    "東風": 90.0,
    "east": 90.0,
    "e": 90.0,
    "南": 180.0,
    "南風": 180.0,
    "south": 180.0,
    "s": 180.0,
    "西": 270.0,
    "西風": 270.0,
    "west": 270.0,
    "w": 270.0,
    "北東": 45.0,
    "北東風": 45.0,
    "northeast": 45.0,
    "ne": 45.0,
    "南東": 135.0,
    "南東風": 135.0,
    "southeast": 135.0,
    "se": 135.0,
    "南西": 225.0,
    "南西風": 225.0,
    "southwest": 225.0,
    "sw": 225.0,
    "北西": 315.0,
    "北西風": 315.0,
    "northwest": 315.0,
    "nw": 315.0,
}
_WIND_DIRECTION_PATTERNS = (
    re.compile(r"風向\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:度|deg|degrees?)?", re.IGNORECASE),
    re.compile(r"wind\s*direction\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:deg|degrees?)?", re.IGNORECASE),
)
_WIND_GUST_PATTERNS = (
    re.compile(r"(?:突風|ガスト)\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:m/s|メートル|mps|キロ|km|ｍ|m)?", re.IGNORECASE),
    re.compile(r"gust\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:m/s|mps|meters?)?", re.IGNORECASE),
)
_WIND_VARIANCE_PATTERNS = (
    re.compile(r"(?:風(?:の)?分散|wind\s*variance)\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE),
    re.compile(r"(?:乱流|turbulence)\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE),
)
_TEMPERATURE_PATTERNS = (
    re.compile(r"(?:気温|温度)\s*(?P<value>-?\d+(?:[.,]\d+)?)\s*(?:度|℃|c|celsius)?", re.IGNORECASE),
    re.compile(r"temperature\s*(?P<value>-?\d+(?:[.,]\d+)?)\s*(?:c|celsius)?", re.IGNORECASE),
)
_PRESSURE_PATTERNS = (
    re.compile(r"(?:気圧)\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:hpa|ヘクトパスカル)?", re.IGNORECASE),
    re.compile(r"pressure\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:hpa)?", re.IGNORECASE),
)
_THERMAL_BATTERY_DRAIN_FACTOR_PATTERNS = (
    re.compile(r"(?:熱|温度|temperature|thermal).{0,16}(?:battery|バッテリー|電池).{0,16}(?:消費|drain|劣化).{0,8}(?P<value>\d+(?:[.,]\d+)?)\s*(?:倍|x|×)?", re.IGNORECASE),
    re.compile(r"(?:battery|バッテリー|電池).{0,16}(?:drain|消費|劣化).{0,8}(?P<value>\d+(?:[.,]\d+)?)\s*(?:倍|x|×)", re.IGNORECASE),
)
_THERMAL_MOTOR_DERATE_FACTOR_PATTERNS = (
    re.compile(r"(?:熱|温度|temperature|thermal).{0,16}(?:motor|モーター|推力).{0,16}(?:derate|制限|低下).{0,8}(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE),
    re.compile(r"(?:motor|モーター|推力).{0,16}(?:derate|制限|低下).{0,8}(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE),
)
_PAYLOAD_WEIGHT_PATTERNS = (
    re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:kg|キロ|ｋｇ)\s*の?\s*(?:荷物|payload)", re.IGNORECASE),
    re.compile(r"(?:荷物|payload)\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?:kg|キロ|ｋｇ)", re.IGNORECASE),
)
_OBSTACLE_REQUEST_PATTERN = re.compile(
    r"(?:障害物|障害|ビル|建物|高層|回避|landing\s*zone\s*blocked|blocked\s*landing|obstacle|building\s*risk|avoid\s*obstacle)",
    re.IGNORECASE,
)
_OBSTACLE_CLEAR_PATTERN = re.compile(
    r"(?:障害物\s*(?:なし|無し|ない|無い)|ビルリスク\s*(?:なし|無し|ない|無い)|no\s+obstacle|without\s+obstacle|landing\s*zone\s*clear)",
    re.IGNORECASE,
)
_POSTAL_CODE_PATTERN = re.compile(
    r"(?:〒|郵便番号\s*)?(?P<code>\d{3})-?(?P<tail>\d{4})"
)
_JAPANESE_TEXT_PATTERN = re.compile(r"[ぁ-んァ-ン一-龥]")
_ROUTE_ARROW_PATTERN = re.compile(
    r"^\s*(?:plan\s+(?:a\s+)?(?:delivery|mission|route)\s+)?"
    r"(?:from\s+)?(?P<origin>.+?)\s*(?:->|=>|→|⇒)\s*(?P<destination>.+?)\s*$",
    re.IGNORECASE,
)
_ROUTE_FROM_TO_PATTERN = re.compile(
    r"\bfrom\s+(?P<origin>.+?)\s+\bto\s+(?P<destination>.+?)\s*$",
    re.IGNORECASE,
)
_ROUTE_PLAIN_TO_PATTERN = re.compile(
    r"^\s*(?P<origin>[^,;]+?)\s+\bto\s+(?P<destination>[^,;]+?)\s*$",
    re.IGNORECASE,
)
_PLACE_QUERY_HINT_WORDS = frozenset(
    {
        "airport",
        "bridge",
        "building",
        "campus",
        "city",
        "hall",
        "hospital",
        "library",
        "museum",
        "park",
        "port",
        "station",
        "terminal",
        "tower",
        "university",
        "駅",
        "空港",
        "橋",
        "図書館",
        "公園",
        "大学",
        "病院",
        "港",
        "市役所",
    }
)


@dataclass(frozen=True)
class _KnownPlace:
    canonical_label: str
    latitude: float
    longitude: float
    aliases: tuple[str, ...]


_DEFAULT_ORIGIN_PLACE = _KnownPlace(
    canonical_label="Tokyo Station",
    latitude=35.681236,
    longitude=139.767125,
    aliases=("東京駅", "tokyo station", "tokyo"),
)
_KNOWN_PLACES = (
    _DEFAULT_ORIGIN_PLACE,
    _KnownPlace(
        canonical_label="Ueno Station",
        latitude=35.713768,
        longitude=139.777254,
        aliases=("上野駅", "上野", "ueno station", "ueno"),
    ),
    _KnownPlace(
        canonical_label="Shinagawa Station",
        latitude=35.628471,
        longitude=139.73876,
        aliases=("品川駅", "品川", "shinagawa station", "shinagawa"),
    ),
    _KnownPlace(
        canonical_label="Yokohama Station",
        latitude=35.465833,
        longitude=139.622311,
        aliases=("横浜駅", "横浜", "yokohama station", "yokohama"),
    ),
    _KnownPlace(
        canonical_label="Kawasaki Station",
        latitude=35.5255,
        longitude=139.6915,
        aliases=("川崎駅", "川崎", "kawasaki station", "kawasaki"),
    ),
)


@dataclass(frozen=True)
class _ResolvedPlace:
    query: str
    label: str
    latitude: float
    longitude: float
    source_url: str
    provider_response_status: str
    display_name: str
    provider_place_id: str
    place_type: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class _PlaceResolution:
    canonical_label: str
    latitude: float
    longitude: float
    alias: str
    source_refs: tuple[str, ...]
    source_payload: Mapping[str, Any] | None = None


def _semantic_timeout_seconds() -> int:
    value = os.environ.get(CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS_ENV, "").strip()
    try:
        parsed = int(value) if value else 10
    except ValueError:
        return 10
    return max(1, parsed)


def _semantic_agent_enabled() -> bool:
    if os.environ.get(CHIEF_ROUTE_SEMANTIC_ADK_ENABLED_ENV, "").strip() == "1":
        return True
    return os.environ.get("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", "").strip() == "1"


def _float_field(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _int_field(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_semantic_route_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    request = raw.get("mission_designer_request")
    if not isinstance(request, Mapping):
        request = raw
    origin_query = str(request.get("origin_query") or "").strip()
    destination_query = str(request.get("destination_query") or "").strip()
    unknowns = request.get("unknowns")
    unknown_list = (
        [str(item)[:200] for item in unknowns]
        if isinstance(unknowns, list)
        else []
    )
    payload: dict[str, Any] = {
        "schema_version": SEMANTIC_ROUTE_REQUEST_SCHEMA_VERSION,
        "tool_name": "missionos_chief_semantic_route_request",
        "tool_status": "resolved" if origin_query or destination_query else "not_applicable",
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "origin_query": origin_query,
        "destination_query": destination_query,
        "payload_weight_kg": _float_field(request.get("payload_weight_kg")),
        "wind_speed_mps": _float_field(request.get("wind_speed_mps")),
        "wind_direction_deg": _float_field(request.get("wind_direction_deg")),
        "wind_gust_mps": _float_field(request.get("wind_gust_mps")),
        "wind_variance": _float_field(request.get("wind_variance")),
        "temperature_c": _float_field(request.get("temperature_c")),
        "pressure_hpa": _float_field(request.get("pressure_hpa")),
        "thermal_battery_drain_factor": _float_field(
            request.get("thermal_battery_drain_factor")
        ),
        "thermal_motor_derate_factor": _float_field(
            request.get("thermal_motor_derate_factor")
        ),
        "wind_speed_unit_interpretation": str(
            request.get("wind_speed_unit_interpretation") or ""
        )[:200],
        "auto_route_waypoint_count": _int_field(
            request.get("auto_route_waypoint_count")
        ),
        "confidence": _float_field(request.get("confidence")),
        "unknowns": unknown_list[:10],
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    payload["sha256"] = _content_hash(payload)
    return payload


def _chief_route_function_tool_prompt_payload(utterance: str) -> dict[str, Any]:
    return {
        "schema_version": "missionos_chief_route_function_tool_prompt.v1",
        "role_contract": {
            "operator_facing_agent": "missionos_chief_agent",
            "task": (
                "Understand the operator's natural-language Mission Designer "
                "route request and call the available ADK function tool with "
                "the natural source-tool arguments."
            ),
            "tool_to_call": CHIEF_ROUTE_FUNCTION_TOOL_NAME,
            "llm_may": [
                "interpret colloquial Japanese and English phrasing",
                "pass origin and destination as natural place queries",
                "normalize payload weight to kg when clearly stated",
                "normalize wind speed to m/s when clearly stated",
                "normalize wind direction to degrees when clearly stated",
                "pass wind gust and wind variance when clearly stated",
                "pass temperature in Celsius and pressure in hPa when clearly stated",
                "pass thermal battery drain and motor derate factors when explicitly requested",
                "treat Japanese operator shorthand like 風速9キロ, 風速10キロ, or 風速Nキロ as N m/s in this MissionOS drone-ops context unless km/h is explicitly written",
                "convert wind speed from km/h only when the utterance explicitly says km/h or kilometers per hour",
                "leave optional arguments null or omitted when ambiguous",
            ],
            "llm_must_not": [
                "approve",
                "create dispatch authority",
                "execute",
                "claim progress",
                "invent coordinates",
                "invent source-backed weather",
                "invent source-backed terrain",
            ],
            "function_tool_owns": [
                "geocoding",
                "postal-code lookup",
                "weather lookup",
                "terrain lookup",
                "coordinate_route artifact construction",
            ],
        },
        "human_utterance": str(utterance or "")[:2000],
    }


def _route_function_tool_status(
    status: str,
    *,
    blocking_reasons: list[str] | None = None,
    agent_invocation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CHIEF_ROUTE_FUNCTION_TOOL_SCHEMA_VERSION,
        "tool_name": CHIEF_ROUTE_FUNCTION_TOOL_NAME,
        "tool_status": status,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "adk_function_tool_called": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    if blocking_reasons:
        payload["blocking_reasons"] = blocking_reasons
    if isinstance(agent_invocation, Mapping):
        payload["agent_invocation"] = dict(agent_invocation)
    payload["sha256"] = _content_hash(payload)
    return payload


def _semantic_route_request_status(status: str) -> dict[str, Any]:
    payload = {
        "schema_version": SEMANTIC_ROUTE_REQUEST_SCHEMA_VERSION,
        "tool_name": "missionos_chief_semantic_route_request",
        "tool_status": status,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    payload["sha256"] = _content_hash(payload)
    return payload


def _attach_route_function_tool_metadata(
    result: Mapping[str, Any],
    *,
    arguments: Mapping[str, Any],
    invocation_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    invocation_payload: dict[str, Any] = {
        "schema_version": CHIEF_ROUTE_FUNCTION_TOOL_SCHEMA_VERSION,
        "tool_name": CHIEF_ROUTE_FUNCTION_TOOL_NAME,
        "tool_status": "called",
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "adk_function_tool_called": True,
        "arguments": dict(arguments),
        "agent_invocation_artifact_path": str(
            invocation_evidence.get("artifact_path") or ""
        ),
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    invocation_payload["sha256"] = _content_hash(invocation_payload)

    enriched = dict(result)
    names = [str(name) for name in enriched.get("internal_tool_names") or []]
    if CHIEF_ROUTE_FUNCTION_TOOL_NAME not in names:
        names.insert(0, CHIEF_ROUTE_FUNCTION_TOOL_NAME)
    enriched["internal_tool_names"] = names
    enriched["chief_route_function_tool_invocation"] = invocation_payload
    enriched["adk_function_tool_called"] = True
    enriched["adk_function_tool_name"] = CHIEF_ROUTE_FUNCTION_TOOL_NAME
    enriched["chief_agent_invocation_ref"] = str(
        invocation_evidence.get("artifact_path") or ""
    )
    enriched["chief_agent_invocation_kind"] = "google_adk_function_tool_call"
    enriched["chief_agent_invocation_sha256"] = str(
        invocation_evidence.get("sha256") or ""
    )
    enriched.pop("planner_tools_hash", None)
    enriched.pop("sha256", None)
    result_hash = _content_hash(enriched)
    enriched["planner_tools_hash"] = result_hash
    enriched["sha256"] = result_hash
    return enriched


async def _invoke_chief_route_function_tool_async(
    *,
    utterance: str,
    now: datetime | None,
    weather_fetcher: Callable[[str], Any] | None,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    terrain_fetcher: Callable[[str], Any] | None,
    weather_timeout_seconds: float,
    place_timeout_seconds: float,
    terrain_timeout_seconds: float,
) -> dict[str, Any]:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.tools import FunctionTool
    from google.genai import types

    from src.agents.model_config import llm_provider_label, resolve_agent_model
    from src.intelligence import missionos_agent_runtime as agent_runtime
    from src.runtime.session_service import create_session_service

    agent_name = "missionos_chief_agent"
    agent_runtime._configure_google_adk_environment(agent_name)
    model_id = agent_runtime._model_id(agent_name)
    prompt_payload = _chief_route_function_tool_prompt_payload(utterance)
    prompt_text = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)
    started_at = _utc(datetime.now(timezone.utc))
    captured: dict[str, Any] = {}

    def missionos_resolve_mission_designer_route(
        origin_query: str = "",
        destination_query: str = "",
        payload_weight_kg: float | None = None,
        wind_speed_mps: float | None = None,
        wind_direction_deg: float | None = None,
        wind_gust_mps: float | None = None,
        wind_variance: float | None = None,
        temperature_c: float | None = None,
        pressure_hpa: float | None = None,
        thermal_battery_drain_factor: float | None = None,
        thermal_motor_derate_factor: float | None = None,
        wind_speed_unit_interpretation: str = "",
        auto_route_waypoint_count: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a MissionOS Mission Designer route from Chief LLM arguments.

        Call this for drone delivery / PX4 / Gazebo / Mission Designer route
        requests. Pass natural place names or postal codes as queries; this
        tool resolves source-backed coordinates, weather, terrain, payload, and
        wind into a bounded planning artifact. This tool never approves,
        dispatches, executes, or counts progress.
        """

        arguments = {
            "origin_query": str(origin_query or "").strip(),
            "destination_query": str(destination_query or "").strip(),
            "payload_weight_kg": _float_field(payload_weight_kg),
            "wind_speed_mps": _float_field(wind_speed_mps),
            "wind_direction_deg": _float_field(wind_direction_deg),
            "wind_gust_mps": _float_field(wind_gust_mps),
            "wind_variance": _float_field(wind_variance),
            "temperature_c": _float_field(temperature_c),
            "pressure_hpa": _float_field(pressure_hpa),
            "thermal_battery_drain_factor": _float_field(
                thermal_battery_drain_factor
            ),
            "thermal_motor_derate_factor": _float_field(
                thermal_motor_derate_factor
            ),
            "wind_speed_unit_interpretation": str(
                wind_speed_unit_interpretation or ""
            )[:200],
            "auto_route_waypoint_count": _int_field(auto_route_waypoint_count),
        }
        semantic_request = _normalize_semantic_route_request(
            {"mission_designer_request": arguments}
        )
        captured["arguments"] = arguments
        result = resolve_chief_planner_internal_tools(
            utterance=utterance,
            now=now,
            semantic_route_request=semantic_request,
            weather_fetcher=weather_fetcher,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            terrain_fetcher=terrain_fetcher,
            weather_timeout_seconds=weather_timeout_seconds,
            place_timeout_seconds=place_timeout_seconds,
            terrain_timeout_seconds=terrain_timeout_seconds,
        )
        captured["result"] = dict(result)
        return dict(result)

    agent = LlmAgent(
        name=agent_name,
        model=resolve_agent_model(model_id, agent_name=agent_name),
        description="MissionOS chief coordinator route source tool caller",
        instruction=(
            "You are the operator-facing MissionOS Chief Agent. For the given "
            "operator route request, call the "
            f"`{CHIEF_ROUTE_FUNCTION_TOOL_NAME}` ADK function tool exactly once "
            "with the natural route, payload, wind, weather, thermal, and waypoint "
            "arguments you understand. Do not invent coordinates, weather, or terrain. Do "
            "not approve, dispatch, execute, or claim progress. After the tool "
            "returns, summarize only that this is planning evidence."
        ),
        tools=[FunctionTool(missionos_resolve_mission_designer_route)],
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
    )
    app_name = "missionos_chief_route_function_tool"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    response_parts: list[str] = []
    function_calls: list[dict[str, Any]] = []
    function_responses: list[dict[str, Any]] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if not event.content:
            continue
        for part in event.content.parts or []:
            text = getattr(part, "text", None)
            if text and event.is_final_response():
                response_parts.append(text)
            function_call = getattr(part, "function_call", None)
            if function_call:
                function_calls.append({
                    "name": str(getattr(function_call, "name", "") or ""),
                    "args": dict(getattr(function_call, "args", None) or {}),
                })
            function_response = getattr(part, "function_response", None)
            if function_response:
                function_responses.append({
                    "name": str(getattr(function_response, "name", "") or ""),
                    "response_present": bool(
                        getattr(function_response, "response", None)
                    ),
                })

    completed_at = _utc(datetime.now(timezone.utc))
    response_text = "".join(response_parts).strip()
    result = captured.get("result")
    arguments = captured.get("arguments")
    evidence: dict[str, Any] = {
        "schema_version": getattr(
            agent_runtime,
            "MISSIONOS_AGENT_INVOCATION_EVIDENCE_SCHEMA_VERSION",
            "missionos_agent_invocation_evidence.v1",
        ),
        "agent_name": agent_name,
        "agent_role": "MissionOS chief route FunctionTool caller",
        "provider": llm_provider_label(agent_name),
        "invocation_kind": "google_adk_function_tool_call",
        "model_id": model_id,
        "prompt_sha256": sha256(prompt_text.encode("utf-8")).hexdigest(),
        "response_sha256": sha256(response_text.encode("utf-8")).hexdigest(),
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "function_tool_name": CHIEF_ROUTE_FUNCTION_TOOL_NAME,
        "function_calls": function_calls,
        "function_responses": function_responses,
        "function_tool_called": isinstance(result, Mapping)
        and isinstance(arguments, Mapping),
        "tool_arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
        "progress_counted": False,
        "llm_judgment_in_gate": False,
        "dispatch_authority_created": False,
    }
    evidence["sha256"] = _content_hash(evidence)
    try:
        evidence["artifact_path"] = agent_runtime._persist_invocation_evidence(
            evidence
        )
    except Exception:
        evidence["artifact_path"] = ""

    if not isinstance(result, Mapping) or not isinstance(arguments, Mapping):
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "not_applicable",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "internal_tool_names": [CHIEF_ROUTE_FUNCTION_TOOL_NAME],
            "chief_route_function_tool_invocation": _route_function_tool_status(
                "not_called",
                agent_invocation=evidence,
            ),
            "adk_function_tool_called": False,
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": _utc(now).isoformat(),
        }

    return _attach_route_function_tool_metadata(
        result,
        arguments=arguments,
        invocation_evidence=evidence,
    )


def _invoke_chief_route_function_tool(
    *,
    utterance: str,
    now: datetime | None,
    weather_fetcher: Callable[[str], Any] | None,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    terrain_fetcher: Callable[[str], Any] | None,
    weather_timeout_seconds: float,
    place_timeout_seconds: float,
    terrain_timeout_seconds: float,
) -> dict[str, Any]:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_chief_route_function_tool_async(
                utterance=utterance,
                now=now,
                weather_fetcher=weather_fetcher,
                postal_fetcher=postal_fetcher,
                geocode_fetcher=geocode_fetcher,
                terrain_fetcher=terrain_fetcher,
                weather_timeout_seconds=weather_timeout_seconds,
                place_timeout_seconds=place_timeout_seconds,
                terrain_timeout_seconds=terrain_timeout_seconds,
            ),
            timeout=_semantic_timeout_seconds(),
        )
    )


def _resolve_chief_route_via_function_tool(
    *,
    utterance: str,
    now: datetime | None,
    weather_fetcher: Callable[[str], Any] | None,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    terrain_fetcher: Callable[[str], Any] | None,
    weather_timeout_seconds: float,
    place_timeout_seconds: float,
    terrain_timeout_seconds: float,
) -> dict[str, Any]:
    if not _semantic_agent_enabled():
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "not_configured",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "internal_tool_names": [CHIEF_ROUTE_FUNCTION_TOOL_NAME],
            "chief_route_function_tool_invocation": _route_function_tool_status(
                "not_configured",
                blocking_reasons=[
                    f"{CHIEF_ROUTE_SEMANTIC_ADK_ENABLED_ENV}_or_MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_not_enabled"
                ],
            ),
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": _utc(now).isoformat(),
        }
    from src.intelligence import missionos_agent_runtime as agent_runtime

    if not agent_runtime._google_adk_credentials_available("missionos_chief_agent"):
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "not_configured",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "internal_tool_names": [CHIEF_ROUTE_FUNCTION_TOOL_NAME],
            "chief_route_function_tool_invocation": _route_function_tool_status(
                "not_configured",
                blocking_reasons=["GOOGLE_API_KEY_not_configured"],
            ),
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": _utc(now).isoformat(),
        }
    try:
        return _invoke_chief_route_function_tool(
            utterance=utterance,
            now=now,
            weather_fetcher=weather_fetcher,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            terrain_fetcher=terrain_fetcher,
            weather_timeout_seconds=weather_timeout_seconds,
            place_timeout_seconds=place_timeout_seconds,
            terrain_timeout_seconds=terrain_timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - live ADK failure shape varies.
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "blocked_source_unavailable",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "internal_tool_names": [CHIEF_ROUTE_FUNCTION_TOOL_NAME],
            "chief_route_function_tool_invocation": _route_function_tool_status(
                "blocked_source_unavailable",
                blocking_reasons=[
                    f"chief_route_function_tool_failed:{type(exc).__name__}"
                ],
            ),
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": _utc(now).isoformat(),
        }


def resolve_chief_semantic_route_request(*, utterance: str) -> dict[str, Any]:
    """Expose the Chief ADK FunctionTool route arguments for legacy callers."""
    result = _resolve_chief_route_via_function_tool(
        utterance=utterance,
        now=None,
        weather_fetcher=None,
        postal_fetcher=None,
        geocode_fetcher=None,
        terrain_fetcher=None,
        weather_timeout_seconds=5.0,
        place_timeout_seconds=5.0,
        terrain_timeout_seconds=5.0,
    )
    semantic_request = result.get("semantic_route_request")
    if isinstance(semantic_request, Mapping):
        payload = dict(semantic_request)
        payload["agent_invocation"] = result.get("chief_route_function_tool_invocation")
        payload["source_ref"] = (
            f"missionos_chief_semantic_route_request:{payload['sha256'][:16]}"
            if payload.get("sha256")
            else ""
        )
        return payload
    invocation = result.get("chief_route_function_tool_invocation")
    blocking_reasons = (
        list(invocation.get("blocking_reasons") or [])
        if isinstance(invocation, Mapping)
        else []
    )
    return {
        "schema_version": SEMANTIC_ROUTE_REQUEST_SCHEMA_VERSION,
        "tool_name": "missionos_chief_semantic_route_request",
        "tool_status": result.get("tool_status") or "not_applicable",
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "blocking_reasons": blocking_reasons,
        "agent_invocation": invocation if isinstance(invocation, Mapping) else {},
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _content_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}_{_content_hash(payload)[:12]}"


def _open_meteo_jma_url(latitude: float, longitude: float) -> str:
    query = urlencode({
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
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
    })
    return f"{OPEN_METEO_JMA_URL_PREFIX}?{query}"


def _open_meteo_elevation_url(points: tuple[tuple[float, float], ...]) -> str:
    query = urlencode({
        "latitude": ",".join(f"{latitude:.6f}" for latitude, _longitude in points),
        "longitude": ",".join(f"{longitude:.6f}" for _latitude, longitude in points),
        "current": "temperature_2m",
        "timezone": "UTC",
        "forecast_days": "1",
    })
    return f"{OPEN_METEO_FORECAST_URL_PREFIX}?{query}"


def _gsi_dem_tile_point(latitude: float, longitude: float, *, zoom: int) -> tuple[int, int, int, int]:
    lat_rad = math.radians(latitude)
    n = 2.0**zoom
    x_float = (longitude + 180.0) / 360.0 * n
    y_float = (
        1.0
        - math.asinh(math.tan(lat_rad)) / math.pi
    ) / 2.0 * n
    tile_x = int(math.floor(x_float))
    tile_y = int(math.floor(y_float))
    pixel_x = int((x_float - tile_x) * 256.0)
    pixel_y = int((y_float - tile_y) * 256.0)
    return (
        tile_x,
        tile_y,
        min(255, max(0, pixel_x)),
        min(255, max(0, pixel_y)),
    )


def _gsi_dem_tile_url(latitude: float, longitude: float, *, zoom: int = GSI_DEM_TILE_ZOOM) -> str:
    tile_x, tile_y, _pixel_x, _pixel_y = _gsi_dem_tile_point(
        latitude,
        longitude,
        zoom=zoom,
    )
    return f"{GSI_DEM_TILE_URL_PREFIX}/{zoom}/{tile_x}/{tile_y}.txt"


def _fetch_weather_payload(
    url: str,
    *,
    fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[str, Mapping[str, Any]]:
    if fetcher is not None:
        fetched = fetcher(url)
        if isinstance(fetched, tuple):
            status = str(fetched[0])
            payload = fetched[1]
        else:
            status = "injected_fetcher"
            payload = fetched
        if isinstance(payload, str):
            payload = json.loads(payload)
        return status, dict(payload)
    request = Request(url, headers={"User-Agent": OPEN_METEO_USER_AGENT})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        status = f"http_{getattr(response, 'status', 200)}"
        return status, json.loads(response.read().decode("utf-8"))


def _fetch_terrain_elevation_payload(
    url: str,
    *,
    fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[str, Any]:
    return _fetch_json_payload(
        url,
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
        user_agent=OPEN_METEO_USER_AGENT,
    )


def _fetch_gsi_dem_tile_payload(
    url: str,
    *,
    fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[str, str]:
    status, payload = _fetch_json_or_text_payload(
        url,
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
        user_agent=GSI_DEM_USER_AGENT,
    )
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str):
        raise ValueError("GSI DEM tile response must be text")
    return status, payload


def _fetch_json_payload(
    url: str,
    *,
    fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
    user_agent: str = SOURCE_TOOL_USER_AGENT,
) -> tuple[str, Any]:
    if fetcher is not None:
        fetched = fetcher(url)
        if isinstance(fetched, tuple):
            status = str(fetched[0])
            payload = fetched[1]
        else:
            status = "injected_fetcher"
            payload = fetched
        if isinstance(payload, str):
            payload = json.loads(payload)
        return status, payload
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        status = f"http_{getattr(response, 'status', 200)}"
        return status, json.loads(response.read().decode("utf-8"))


def _fetch_json_or_text_payload(
    url: str,
    *,
    fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
    user_agent: str = SOURCE_TOOL_USER_AGENT,
) -> tuple[str, Any]:
    if fetcher is not None:
        fetched = fetcher(url)
        if isinstance(fetched, tuple):
            return str(fetched[0]), fetched[1]
        return "injected_fetcher", fetched
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        status = f"http_{getattr(response, 'status', 200)}"
        return status, response.read().decode("utf-8")


def _find_known_place(
    text: str, *, after_index: int = 0
) -> tuple[_KnownPlace, str, int] | None:
    lowered = text.lower()
    matches: list[tuple[int, _KnownPlace, str]] = []
    for place in _KNOWN_PLACES:
        for alias in place.aliases:
            index = lowered.find(alias.lower(), after_index)
            if index >= 0:
                matches.append((index, place, alias))
    if not matches:
        return None
    index, place, alias = min(matches, key=lambda item: item[0])
    return place, alias, index


def _normalize_postal_code(code: str, tail: str | None = None) -> str:
    normalized = str(code or "").translate(_FULLWIDTH_NUMBER_TRANSLATION)
    digits = "".join(ch for ch in f"{normalized}{tail or ''}" if ch.isdigit())
    if len(digits) != 7:
        raise ValueError("postal code must contain seven digits")
    return digits


def _postal_search_url(postal_code: str) -> str:
    return f"{ZIPCLOUD_SEARCH_URL}?{urlencode({'zipcode': postal_code})}"


def _query_has_japan_context(query: str) -> bool:
    text = str(query or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return bool(_JAPANESE_TEXT_PATTERN.search(text)) or "日本" in text or bool(
        re.search(r"\b(?:japan|jp)\b", lowered)
    )


def _nominatim_place_query(query: str) -> str:
    text = str(query or "").strip()
    if _query_has_japan_context(text) and "japan" not in text.lower() and "日本" not in text:
        return f"{text}, Japan"
    return text


def _nominatim_search_url(
    query: str,
    *,
    countrycodes: str | None = None,
    language: str = "en",
) -> str:
    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "accept-language": language,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes
    return f"{NOMINATIM_SEARCH_URL}?{urlencode(params)}"


def _known_place_resolution(place: _KnownPlace, alias: str) -> _PlaceResolution:
    return _PlaceResolution(
        canonical_label=place.canonical_label,
        latitude=place.latitude,
        longitude=place.longitude,
        alias=alias,
        source_refs=(f"place_registry:{place.canonical_label}",),
    )


def _default_origin_resolution() -> _PlaceResolution:
    return _known_place_resolution(
        _DEFAULT_ORIGIN_PLACE,
        "default_origin:Tokyo Station",
    )


def _resolve_postal_code_place(
    postal_code: str,
    *,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> _PlaceResolution:
    normalized_code = _normalize_postal_code(postal_code)
    postal_url = _postal_search_url(normalized_code)
    postal_status, postal_payload = _fetch_json_payload(
        postal_url,
        fetcher=postal_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(postal_payload, Mapping):
        raise ValueError("postal response must be an object")
    results = postal_payload.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("postal code not found")
    first = results[0]
    if not isinstance(first, Mapping):
        raise ValueError("postal result must be an object")
    address_parts = [
        str(first.get(key) or "").strip()
        for key in ("address1", "address2", "address3")
    ]
    address = "".join(part for part in address_parts if part)
    if not address:
        raise ValueError("postal result missing address")

    geocode_url = _nominatim_search_url(f"{address}, Japan", countrycodes="jp")
    geocode_status, geocode_payload = _fetch_json_payload(
        geocode_url,
        fetcher=geocode_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(geocode_payload, list) or not geocode_payload:
        raise ValueError("postal address geocode not found")
    geocode_first = geocode_payload[0]
    if not isinstance(geocode_first, Mapping):
        raise ValueError("postal address geocode result must be an object")
    latitude = _optional_float(geocode_first.get("lat"))
    longitude = _optional_float(geocode_first.get("lon"))
    if latitude is None or longitude is None:
        raise ValueError("postal address geocode missing lat/lon")

    resolver_payload = {
        "schema_version": POSTAL_CODE_RESOLVER_TOOL_SCHEMA_VERSION,
        "tool_name": "missionos_postal_code_resolver_tool",
        "tool_status": "resolved",
        "provider": "zipcloud_plus_nominatim",
        "postal_code": normalized_code,
        "postal_source_url": postal_url,
        "postal_provider_response_status": postal_status,
        "address": address,
        "address_parts": address_parts,
        "geocode_source_url": geocode_url,
        "geocode_provider_response_status": geocode_status,
        "display_name": str(geocode_first.get("display_name") or address),
        "provider_place_id": str(geocode_first.get("place_id") or ""),
        "place_type": str(geocode_first.get("type") or ""),
        "latitude": latitude,
        "longitude": longitude,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    resolver_hash = _content_hash(resolver_payload)
    resolver_payload = {
        **resolver_payload,
        "postal_resolver_hash": resolver_hash,
        "sha256": resolver_hash,
    }
    label = f"Postal {normalized_code} ({address})"
    return _PlaceResolution(
        canonical_label=label,
        latitude=latitude,
        longitude=longitude,
        alias=f"postal_code:{normalized_code}",
        source_refs=(f"missionos_postal_code_resolver_tool_result:{resolver_hash[:16]}",),
        source_payload=resolver_payload,
    )


def _resolve_geocoded_place(
    query: str,
    *,
    geocode_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> _PlaceResolution:
    cleaned_query = str(query or "").strip()
    if not cleaned_query:
        raise ValueError("place query must not be empty")
    geocode_countrycodes = "jp" if _query_has_japan_context(cleaned_query) else ""
    geocode_query = _nominatim_place_query(cleaned_query)
    geocode_url = _nominatim_search_url(
        geocode_query,
        countrycodes=geocode_countrycodes or None,
    )
    geocode_status, geocode_payload = _fetch_json_payload(
        geocode_url,
        fetcher=geocode_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(geocode_payload, list) or not geocode_payload:
        raise ValueError("place geocode not found")
    first = geocode_payload[0]
    if not isinstance(first, Mapping):
        raise ValueError("place geocode result must be an object")
    latitude = _optional_float(first.get("lat"))
    longitude = _optional_float(first.get("lon"))
    if latitude is None or longitude is None:
        raise ValueError("place geocode missing lat/lon")
    resolver_payload = {
        "schema_version": PLACE_GEOCODER_TOOL_SCHEMA_VERSION,
        "tool_name": "missionos_place_geocoder_tool",
        "tool_status": "resolved",
        "provider": "nominatim",
        "query": cleaned_query,
        "geocode_query": geocode_query,
        "geocode_countrycodes": geocode_countrycodes,
        "geocode_language": "en",
        "source_url": geocode_url,
        "provider_response_status": geocode_status,
        "display_name": str(first.get("display_name") or cleaned_query),
        "provider_place_id": str(first.get("place_id") or ""),
        "place_type": str(first.get("type") or ""),
        "latitude": latitude,
        "longitude": longitude,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    resolver_hash = _content_hash(resolver_payload)
    resolver_payload = {
        **resolver_payload,
        "place_geocoder_hash": resolver_hash,
        "sha256": resolver_hash,
    }
    return _PlaceResolution(
        canonical_label=str(first.get("display_name") or cleaned_query),
        latitude=latitude,
        longitude=longitude,
        alias=f"geocode:{cleaned_query}",
        source_refs=(f"missionos_place_geocoder_tool_result:{resolver_hash[:16]}",),
        source_payload=resolver_payload,
    )


def _find_postal_place(
    text: str,
    *,
    after_index: int = 0,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[_PlaceResolution, str, int] | None:
    for match in _POSTAL_CODE_PATTERN.finditer(text, after_index):
        code = _normalize_postal_code(match.group("code"), match.group("tail"))
        place = _resolve_postal_code_place(
            code,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            timeout_seconds=timeout_seconds,
        )
        return place, match.group(0), match.start()
    return None


def _find_route_place(
    text: str,
    *,
    after_index: int = 0,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[_PlaceResolution, str, int] | None:
    candidates: list[tuple[int, _PlaceResolution, str]] = []
    known = _find_known_place(text, after_index=after_index)
    if known is not None:
        place, alias, index = known
        candidates.append((index, _known_place_resolution(place, alias), alias))
    postal = _find_postal_place(
        text,
        after_index=after_index,
        postal_fetcher=postal_fetcher,
        geocode_fetcher=geocode_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if postal is not None:
        place, alias, index = postal
        candidates.append((index, place, alias))
    if not candidates:
        return None
    index, place, alias = min(candidates, key=lambda item: item[0])
    return place, alias, index


def _resolve_route_place_query(
    query: str,
    *,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[_PlaceResolution, str, int] | None:
    text = str(query or "").strip()
    if not text:
        return None
    place = _find_route_place(
        text,
        postal_fetcher=postal_fetcher,
        geocode_fetcher=geocode_fetcher,
        timeout_seconds=timeout_seconds,
    )
    if place is not None:
        return place
    geocoded = _resolve_geocoded_place(
        text,
        geocode_fetcher=geocode_fetcher,
        timeout_seconds=timeout_seconds,
    )
    return geocoded, geocoded.alias, 0


def _clean_route_query(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    cleaned = re.sub(
        r"^(?:plan|create|make)\s+(?:a\s+)?(?:delivery|mission|route)\s+(?:from\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:with|using|carrying)\s+.+$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" \t\n\r.,;:!?")


def _route_query_looks_like_place(value: str) -> bool:
    query = _clean_route_query(value)
    if not query:
        return False
    lowered = query.lower()
    if _JAPANESE_TEXT_PATTERN.search(query):
        return True
    if any(ch.isdigit() for ch in query):
        return True
    if any(hint in lowered for hint in _PLACE_QUERY_HINT_WORDS):
        return True
    words = [word for word in re.split(r"\s+", query) if word]
    return bool(words) and any(word[:1].isupper() for word in words)


def _explicit_route_queries(text: str) -> tuple[str, str] | None:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return None
    for pattern in (_ROUTE_ARROW_PATTERN, _ROUTE_FROM_TO_PATTERN):
        match = pattern.search(normalized)
        if not match:
            continue
        origin = _clean_route_query(match.group("origin"))
        destination = _clean_route_query(match.group("destination"))
        if origin and destination and origin.lower() != destination.lower():
            return origin, destination
    plain_match = _ROUTE_PLAIN_TO_PATTERN.search(normalized)
    if plain_match:
        origin = _clean_route_query(plain_match.group("origin"))
        destination = _clean_route_query(plain_match.group("destination"))
        if (
            origin
            and destination
            and origin.lower() != destination.lower()
            and _route_query_looks_like_place(origin)
            and _route_query_looks_like_place(destination)
        ):
            return origin, destination
    return None


def _resolve_origin_destination(
    text: str,
    *,
    postal_fetcher: Callable[[str], Any] | None,
    geocode_fetcher: Callable[[str], Any] | None,
    place_timeout_seconds: float,
    semantic_route_request: Mapping[str, Any] | None = None,
) -> tuple[_PlaceResolution, str, _PlaceResolution, str] | None:
    if isinstance(semantic_route_request, Mapping):
        origin_query = str(semantic_route_request.get("origin_query") or "").strip()
        destination_query = str(
            semantic_route_request.get("destination_query") or ""
        ).strip()
        if origin_query or destination_query:
            origin_match = (
                _resolve_route_place_query(
                    origin_query,
                    postal_fetcher=postal_fetcher,
                    geocode_fetcher=geocode_fetcher,
                    timeout_seconds=place_timeout_seconds,
                )
                if origin_query
                else None
            )
            destination_match = (
                _resolve_route_place_query(
                    destination_query,
                    postal_fetcher=postal_fetcher,
                    geocode_fetcher=geocode_fetcher,
                    timeout_seconds=place_timeout_seconds,
                )
                if destination_query
                else None
            )
            if origin_match is None and destination_match is not None:
                origin_match = (
                    _default_origin_resolution(),
                    "default_origin:Tokyo Station",
                    0,
                )
            if origin_match is not None and destination_match is not None:
                origin, origin_alias, _ = origin_match
                destination, destination_alias, _ = destination_match
                if origin.canonical_label != destination.canonical_label:
                    return origin, origin_alias, destination, destination_alias

    explicit_queries = _explicit_route_queries(text)
    if explicit_queries is not None:
        origin_query, destination_query = explicit_queries
        origin_match = _resolve_route_place_query(
            origin_query,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            timeout_seconds=place_timeout_seconds,
        )
        destination_match = _resolve_route_place_query(
            destination_query,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            timeout_seconds=place_timeout_seconds,
        )
        if origin_match is not None and destination_match is not None:
            origin, origin_alias, _ = origin_match
            destination, destination_alias, _ = destination_match
            if origin.canonical_label != destination.canonical_label:
                return origin, origin_alias, destination, destination_alias

    origin_match = _find_route_place(
        text,
        postal_fetcher=postal_fetcher,
        geocode_fetcher=geocode_fetcher,
        timeout_seconds=place_timeout_seconds,
    )
    if origin_match is None:
        return None
    origin, origin_alias, origin_index = origin_match
    route_words = ("から", "まで", "へ", "to", "deliver", "delivery", "配送", "届け", "走らせ")
    if not any(word in text.lower() for word in route_words):
        return None
    destination_match = _find_route_place(
        text,
        after_index=origin_index + len(origin_alias),
        postal_fetcher=postal_fetcher,
        geocode_fetcher=geocode_fetcher,
        timeout_seconds=place_timeout_seconds,
    )
    if destination_match is None:
        if (
            origin.canonical_label != _DEFAULT_ORIGIN_PLACE.canonical_label
            and any(word in text.lower() for word in ("まで", "へ", "to", "配送", "届け", "走らせ"))
        ):
            default_origin = _default_origin_resolution()
            return default_origin, default_origin.alias, origin, origin_alias
        return None
    destination, destination_alias, _ = destination_match
    if origin.canonical_label == destination.canonical_label:
        return None
    return origin, origin_alias, destination, destination_alias


def _operator_requested_wind_speed_mps(text: str) -> float | None:
    normalized = text.translate(_FULLWIDTH_NUMBER_TRANSLATION)
    for pattern in _WIND_SPEED_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        try:
            value = float(match.group("value").replace(",", ""))
        except ValueError:
            continue
        if value < 0:
            return None
        return round(value, 3)
    return None


def _operator_requested_pattern_float(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
    *,
    low: float | None = None,
    high: float | None = None,
) -> float | None:
    normalized = text.translate(_FULLWIDTH_NUMBER_TRANSLATION)
    for pattern in patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        try:
            value = float(match.group("value").replace(",", ""))
        except ValueError:
            continue
        if low is not None and value < low:
            return None
        if high is not None and value > high:
            return None
        return round(value, 3)
    return None


def _operator_requested_wind_direction_deg(text: str) -> float | None:
    value = _operator_requested_pattern_float(
        text,
        _WIND_DIRECTION_PATTERNS,
        low=0.0,
        high=360.0,
    )
    if value is not None:
        return value
    normalized = text.translate(_FULLWIDTH_NUMBER_TRANSLATION).lower()
    for alias, direction in sorted(
        _WIND_DIRECTION_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        escaped = re.escape(alias.lower())
        if alias.endswith("風") and re.search(rf"{escaped}", normalized):
            return direction
        if re.search(rf"(?:風向|wind\s*direction)\s*[:=]?\s*{escaped}\b", normalized):
            return direction
    return None


def _operator_requested_wind_gust_mps(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _WIND_GUST_PATTERNS,
        low=0.0,
        high=100.0,
    )


def _operator_requested_wind_variance(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _WIND_VARIANCE_PATTERNS,
        low=0.0,
        high=100.0,
    )


def _operator_requested_temperature_c(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _TEMPERATURE_PATTERNS,
        low=-80.0,
        high=80.0,
    )


def _operator_requested_pressure_hpa(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _PRESSURE_PATTERNS,
        low=500.0,
        high=1100.0,
    )


def _operator_requested_thermal_battery_drain_factor(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _THERMAL_BATTERY_DRAIN_FACTOR_PATTERNS,
        low=0.1,
        high=10.0,
    )


def _operator_requested_thermal_motor_derate_factor(text: str) -> float | None:
    return _operator_requested_pattern_float(
        text,
        _THERMAL_MOTOR_DERATE_FACTOR_PATTERNS,
        low=0.1,
        high=1.0,
    )


def _operator_requested_payload_weight_kg(text: str) -> float | None:
    normalized = text.translate(_FULLWIDTH_NUMBER_TRANSLATION)
    for pattern in _PAYLOAD_WEIGHT_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        try:
            value = float(match.group("value").replace(",", ""))
        except ValueError:
            continue
        if value <= 0:
            return None
        return round(value, 3)
    return None


def _operator_requested_obstacle_flags(text: str) -> dict[str, Any]:
    normalized = str(text or "").translate(_FULLWIDTH_NUMBER_TRANSLATION)
    if _OBSTACLE_CLEAR_PATTERN.search(normalized):
        return {
            "landing_zone_blocked": False,
            "building_risk_detected": False,
            "obstacle_scenario_source": "operator_instruction_clear",
        }
    if not _OBSTACLE_REQUEST_PATTERN.search(normalized):
        return {}
    return {
        "landing_zone_blocked": True,
        "building_risk_detected": True,
        "obstacle_scenario_source": "operator_instruction_bounded_sitl_scenario",
        "gazebo_obstacle_model_spawn_requested": True,
    }


def _apply_operator_requested_obstacle_flags(
    route: dict[str, Any],
    text: str,
) -> None:
    flags = _operator_requested_obstacle_flags(text)
    if not flags:
        return
    route.update(flags)
    refs = [str(ref) for ref in route.get("source_refs") or [] if str(ref).strip()]
    ref = "operator_instruction_obstacle_scenario:bounded_sitl"
    if ref not in refs:
        refs.append(ref)
    route["source_refs"] = refs


def _haversine_m(
    *,
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> float:
    radius_m = 6_371_000.0
    lat1 = math.radians(from_latitude)
    lat2 = math.radians(to_latitude)
    dlat = math.radians(to_latitude - from_latitude)
    dlon = math.radians(to_longitude - from_longitude)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _route_sample_points(
    *,
    origin: _PlaceResolution,
    destination: _PlaceResolution,
    sample_count: int = DEFAULT_TERRAIN_PROFILE_SAMPLE_COUNT,
) -> tuple[dict[str, float], ...]:
    count = max(2, int(sample_count))
    samples: list[dict[str, float]] = []
    for index in range(count):
        fraction = index / (count - 1)
        samples.append(
            {
                "fraction": round(fraction, 6),
                "latitude": round(
                    origin.latitude + (destination.latitude - origin.latitude) * fraction,
                    7,
                ),
                "longitude": round(
                    origin.longitude
                    + (destination.longitude - origin.longitude) * fraction,
                    7,
                ),
            }
        )
    return tuple(samples)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _elevations_from_open_meteo_payload(payload: Any) -> list[float]:
    records = payload if isinstance(payload, list) else [payload]
    elevations: list[float] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        elevation = _optional_float(record.get("elevation"))
        if elevation is not None:
            elevations.append(elevation)
    return elevations


def _elevation_from_gsi_dem_tile(
    tile_text: str,
    *,
    pixel_x: int,
    pixel_y: int,
) -> float:
    rows = [row.strip() for row in tile_text.splitlines() if row.strip()]
    if not rows:
        raise ValueError("GSI DEM tile is empty")
    if pixel_y >= len(rows):
        raise ValueError("GSI DEM tile missing requested row")
    columns = [value.strip() for value in rows[pixel_y].split(",")]
    if pixel_x >= len(columns):
        raise ValueError("GSI DEM tile missing requested column")
    raw = columns[pixel_x]
    if raw.lower() in {"e", "nan", "null", ""}:
        raise ValueError("GSI DEM tile has no-data at requested pixel")
    return float(raw)


def _resolve_route_terrain_profile_from_gsi(
    *,
    samples: tuple[dict[str, float], ...],
    planned_route_m: float,
    terrain_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[str, str, tuple[dict[str, float], ...], list[dict[str, Any]]]:
    tile_cache: dict[str, tuple[str, str]] = {}
    tile_refs: list[dict[str, Any]] = []
    profile: list[dict[str, float]] = []
    statuses: list[str] = []
    for sample in samples:
        latitude = float(sample["latitude"])
        longitude = float(sample["longitude"])
        tile_x, tile_y, pixel_x, pixel_y = _gsi_dem_tile_point(
            latitude,
            longitude,
            zoom=GSI_DEM_TILE_ZOOM,
        )
        source_url = f"{GSI_DEM_TILE_URL_PREFIX}/{GSI_DEM_TILE_ZOOM}/{tile_x}/{tile_y}.txt"
        if source_url not in tile_cache:
            tile_cache[source_url] = _fetch_gsi_dem_tile_payload(
                source_url,
                fetcher=terrain_fetcher,
                timeout_seconds=timeout_seconds,
            )
        provider_response_status, tile_text = tile_cache[source_url]
        statuses.append(provider_response_status)
        elevation = _elevation_from_gsi_dem_tile(
            tile_text,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
        )
        tile_refs.append(
            {
                "source_url": source_url,
                "zoom": GSI_DEM_TILE_ZOOM,
                "tile_x": tile_x,
                "tile_y": tile_y,
                "pixel_x": pixel_x,
                "pixel_y": pixel_y,
                "provider_response_status": provider_response_status,
            }
        )
        profile.append(
            {
                **sample,
                "distance_m": round(planned_route_m * sample["fraction"], 3),
                "terrain_elevation_m": round(float(elevation), 3),
            }
        )
    return (
        "source_backed_gsi_dem_captured",
        ",".join(sorted(set(statuses))) or "not_requested",
        tuple(profile),
        tile_refs,
    )


def _resolve_route_terrain_profile_from_open_meteo(
    *,
    samples: tuple[dict[str, float], ...],
    points: tuple[tuple[float, float], ...],
    planned_route_m: float,
    terrain_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[str, tuple[dict[str, float], ...]]:
    source_url = _open_meteo_elevation_url(points)
    provider_response_status, payload = _fetch_terrain_elevation_payload(
        source_url,
        fetcher=terrain_fetcher,
        timeout_seconds=timeout_seconds,
    )
    elevations = _elevations_from_open_meteo_payload(payload)
    if len(elevations) != len(samples):
        raise ValueError("Open-Meteo elevation response sample count mismatch")
    profile = []
    for sample, elevation in zip(samples, elevations, strict=True):
        profile.append(
            {
                **sample,
                "distance_m": round(planned_route_m * sample["fraction"], 3),
                "terrain_elevation_m": round(float(elevation), 3),
            }
        )
    return provider_response_status, tuple(profile)


def _resolve_route_terrain_profile(
    *,
    origin: _PlaceResolution,
    destination: _PlaceResolution,
    planned_route_m: float,
    resolved_at: datetime,
    terrain_fetcher: Callable[[str], Any] | None,
    timeout_seconds: float,
) -> tuple[dict[str, Any], tuple[dict[str, float], ...]]:
    samples = _route_sample_points(origin=origin, destination=destination)
    points = tuple((sample["latitude"], sample["longitude"]) for sample in samples)
    source_url = "|".join(
        _gsi_dem_tile_url(latitude, longitude)
        for latitude, longitude in points
    )
    status = "source_backed_terrain_captured"
    provider = "gsi_dem_elevation_tiles"
    provider_response_status = "not_requested"
    source_backed_terrain = True
    source_unavailable = False
    primary_provider = "gsi_dem_elevation_tiles"
    primary_source_url = source_url
    primary_provider_response_status = "not_requested"
    gsi_tile_refs: list[dict[str, Any]] = []
    terrain_profile: tuple[dict[str, float], ...] = ()
    try:
        (
            status,
            provider_response_status,
            terrain_profile,
            gsi_tile_refs,
        ) = _resolve_route_terrain_profile_from_gsi(
            samples=samples,
            planned_route_m=planned_route_m,
            terrain_fetcher=terrain_fetcher,
            timeout_seconds=timeout_seconds,
        )
        primary_provider_response_status = provider_response_status
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        primary_provider_response_status = f"source_unavailable:{type(exc).__name__}"
        fallback_source_url = _open_meteo_elevation_url(points)
        try:
            fallback_status, terrain_profile = _resolve_route_terrain_profile_from_open_meteo(
                samples=samples,
                points=points,
                planned_route_m=planned_route_m,
                terrain_fetcher=terrain_fetcher,
                timeout_seconds=timeout_seconds,
            )
            status = "source_backed_terrain_captured_with_gsi_fallback"
            provider = "open_meteo_forecast_elevation_fallback"
            provider_response_status = fallback_status
            source_url = fallback_source_url
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as fallback_exc:
            status = "blocked_source_unavailable"
            provider = "source_backed_terrain_unavailable"
            provider_response_status = (
                f"gsi_source_unavailable:{type(exc).__name__};"
                f"fallback_source_unavailable:{type(fallback_exc).__name__}"
            )
            source_url = f"{primary_source_url}|fallback:{fallback_source_url}"
            source_backed_terrain = False
            source_unavailable = True

    terrain_hash_payload = {
        "provider": provider,
        "source_url": source_url,
        "primary_provider": primary_provider,
        "primary_source_url": primary_source_url,
        "primary_provider_response_status": primary_provider_response_status,
        "tool_status": status,
        "provider_response_status": provider_response_status,
        "source_backed_terrain": source_backed_terrain,
        "source_unavailable": source_unavailable,
        "gsi_tile_refs": gsi_tile_refs,
        "terrain_profile": terrain_profile,
    }
    terrain_hash = _content_hash(terrain_hash_payload)
    tool = {
        "schema_version": TERRAIN_ELEVATION_RESOLVER_TOOL_SCHEMA_VERSION,
        "tool_name": "missionos_terrain_elevation_resolver_tool",
        "tool_status": status,
        "provider": provider,
        "source_url": source_url,
        "primary_provider": primary_provider,
        "primary_source_url": primary_source_url,
        "primary_provider_response_status": primary_provider_response_status,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "captured_at": resolved_at.isoformat(),
        "provider_response_status": provider_response_status,
        "source_backed_terrain": source_backed_terrain,
        "source_unavailable": source_unavailable,
        "terrain_clearance_agl_m": DEFAULT_TERRAIN_CLEARANCE_AGL_M,
        "terrain_profile_sample_count": len(terrain_profile),
        "terrain_profile": list(terrain_profile),
        "gsi_tile_refs": gsi_tile_refs,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "terrain_hash": terrain_hash,
        "sha256": terrain_hash,
    }
    return tool, terrain_profile


def _route_ref(route: Mapping[str, Any]) -> str:
    route_id = str(route.get("route_id") or _stable_id("route", route))
    return f"missionos_chief_coordinate_route:{route_id}"


def enrich_coordinate_route_with_terrain_profile(
    coordinate_route: Mapping[str, Any],
    *,
    now: datetime | None = None,
    terrain_fetcher: Callable[[str], Any] | None = None,
    terrain_timeout_seconds: float = 5.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach source-backed terrain profile evidence to an explicit route."""
    route = dict(coordinate_route)
    if route.get("terrain_profile"):
        return route, {}

    takeoff_latitude = _optional_float(route.get("takeoff_latitude"))
    takeoff_longitude = _optional_float(route.get("takeoff_longitude"))
    dropoff_latitude = _optional_float(route.get("dropoff_latitude"))
    dropoff_longitude = _optional_float(route.get("dropoff_longitude"))
    if (
        takeoff_latitude is None
        or takeoff_longitude is None
        or dropoff_latitude is None
        or dropoff_longitude is None
    ):
        return route, {}

    origin = _PlaceResolution(
        canonical_label=str(route.get("takeoff_label") or "Operator takeoff"),
        latitude=takeoff_latitude,
        longitude=takeoff_longitude,
        alias=str(route.get("takeoff_label") or "operator_takeoff"),
        source_refs=tuple(str(ref) for ref in route.get("source_refs") or ()),
    )
    destination = _PlaceResolution(
        canonical_label=str(route.get("dropoff_label") or "Operator dropoff"),
        latitude=dropoff_latitude,
        longitude=dropoff_longitude,
        alias=str(route.get("dropoff_label") or "operator_dropoff"),
        source_refs=tuple(str(ref) for ref in route.get("source_refs") or ()),
    )
    planned_route_m = (
        _optional_float(route.get("planned_route_m"))
        or _optional_float(route.get("derived_route_distance_m"))
        or _haversine_m(
            from_latitude=takeoff_latitude,
            from_longitude=takeoff_longitude,
            to_latitude=dropoff_latitude,
            to_longitude=dropoff_longitude,
        )
    )
    terrain_tool, terrain_profile = _resolve_route_terrain_profile(
        origin=origin,
        destination=destination,
        planned_route_m=planned_route_m,
        resolved_at=_utc(now),
        terrain_fetcher=terrain_fetcher,
        timeout_seconds=terrain_timeout_seconds,
    )
    if terrain_profile:
        route["terrain_profile"] = [dict(sample) for sample in terrain_profile]
        route["terrain_clearance_agl_m"] = float(
            route.get("terrain_clearance_agl_m")
            or route.get("terrain_clearance_target_m")
            or DEFAULT_TERRAIN_CLEARANCE_AGL_M
        )
        route["terrain_profile_source"] = terrain_tool["provider"]
        route["terrain_profile_ref"] = (
            f"missionos_terrain_elevation_resolver_tool_result:{terrain_tool['sha256'][:16]}"
        )
        existing_refs = [
            str(ref)
            for ref in route.get("source_refs") or []
            if str(ref).strip()
        ]
        terrain_ref = route["terrain_profile_ref"]
        route["source_refs"] = [
            *existing_refs,
            *([] if terrain_ref in existing_refs else [terrain_ref]),
        ]
    return route, terrain_tool


def resolve_chief_planner_internal_tools(
    *,
    utterance: str,
    now: datetime | None = None,
    semantic_route_request: Mapping[str, Any] | None = None,
    weather_fetcher: Callable[[str], Any] | None = None,
    postal_fetcher: Callable[[str], Any] | None = None,
    geocode_fetcher: Callable[[str], Any] | None = None,
    terrain_fetcher: Callable[[str], Any] | None = None,
    weather_timeout_seconds: float = 5.0,
    place_timeout_seconds: float = 5.0,
    terrain_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Resolve route/weather evidence for Chief without exposing sub-agents."""
    resolved_at = _utc(now)
    text = str(utterance or "").strip()
    if not isinstance(semantic_route_request, Mapping) and _semantic_agent_enabled():
        function_tool_result = _resolve_chief_route_via_function_tool(
            utterance=text,
            now=resolved_at,
            weather_fetcher=weather_fetcher,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            terrain_fetcher=terrain_fetcher,
            weather_timeout_seconds=weather_timeout_seconds,
            place_timeout_seconds=place_timeout_seconds,
            terrain_timeout_seconds=terrain_timeout_seconds,
        )
        if function_tool_result.get("tool_status") in {"resolved", "partial"}:
            return function_tool_result

    semantic_request = (
        dict(semantic_route_request)
        if isinstance(semantic_route_request, Mapping)
        else _semantic_route_request_status("not_configured")
    )
    try:
        match = _resolve_origin_destination(
            text,
            postal_fetcher=postal_fetcher,
            geocode_fetcher=geocode_fetcher,
            place_timeout_seconds=place_timeout_seconds,
            semantic_route_request=semantic_request,
        )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "blocked_source_unavailable",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "internal_tool_names": [
                "missionos_postal_code_resolver_tool",
            ],
            "route_resolver": {
                "schema_version": ROUTE_RESOLVER_TOOL_SCHEMA_VERSION,
                "tool_name": "missionos_route_resolver_tool",
                "tool_status": "blocked_source_unavailable",
                "provider": "postal_code_route_source_unavailable",
                "provider_response_status": f"source_unavailable:{type(exc).__name__}",
                "operator_facing_agent": "missionos_chief_agent",
                "subagents_operator_facing": False,
                "dispatch_authority_created": False,
                "progress_counted": False,
            },
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": resolved_at.isoformat(),
        }
    if match is None:
        return {
            "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
            "tool_status": "not_applicable",
            "operator_facing_agent": "missionos_chief_agent",
            "subagents_operator_facing": False,
            "dispatch_authority_created": False,
            "progress_counted": False,
            "resolved_at": resolved_at.isoformat(),
        }

    origin, origin_alias, destination, destination_alias = match
    source_payloads = [
        payload
        for payload in (origin.source_payload, destination.source_payload)
        if isinstance(payload, Mapping)
    ]
    postal_payloads = [
        payload
        for payload in source_payloads
        if payload.get("schema_version") == POSTAL_CODE_RESOLVER_TOOL_SCHEMA_VERSION
    ]
    geocode_payloads = [
        payload
        for payload in source_payloads
        if payload.get("schema_version") == PLACE_GEOCODER_TOOL_SCHEMA_VERSION
    ]
    requested_wind_speed_mps = _float_field(semantic_request.get("wind_speed_mps"))
    if requested_wind_speed_mps is None:
        requested_wind_speed_mps = _operator_requested_wind_speed_mps(text)
    requested_wind_direction_deg = _float_field(semantic_request.get("wind_direction_deg"))
    if requested_wind_direction_deg is None:
        requested_wind_direction_deg = _operator_requested_wind_direction_deg(text)
    requested_wind_gust_mps = _float_field(semantic_request.get("wind_gust_mps"))
    if requested_wind_gust_mps is None:
        requested_wind_gust_mps = _operator_requested_wind_gust_mps(text)
    requested_wind_variance = _float_field(semantic_request.get("wind_variance"))
    if requested_wind_variance is None:
        requested_wind_variance = _operator_requested_wind_variance(text)
    requested_temperature_c = _float_field(semantic_request.get("temperature_c"))
    if requested_temperature_c is None:
        requested_temperature_c = _operator_requested_temperature_c(text)
    requested_pressure_hpa = _float_field(semantic_request.get("pressure_hpa"))
    if requested_pressure_hpa is None:
        requested_pressure_hpa = _operator_requested_pressure_hpa(text)
    requested_thermal_battery_drain_factor = _float_field(
        semantic_request.get("thermal_battery_drain_factor")
    )
    if requested_thermal_battery_drain_factor is None:
        requested_thermal_battery_drain_factor = (
            _operator_requested_thermal_battery_drain_factor(text)
        )
    requested_thermal_motor_derate_factor = _float_field(
        semantic_request.get("thermal_motor_derate_factor")
    )
    if requested_thermal_motor_derate_factor is None:
        requested_thermal_motor_derate_factor = (
            _operator_requested_thermal_motor_derate_factor(text)
        )
    requested_payload_weight_kg = _float_field(semantic_request.get("payload_weight_kg"))
    if requested_payload_weight_kg is None:
        requested_payload_weight_kg = _operator_requested_payload_weight_kg(text)
    route_distance_m = _haversine_m(
        from_latitude=origin.latitude,
        from_longitude=origin.longitude,
        to_latitude=destination.latitude,
        to_longitude=destination.longitude,
    )
    route_result_payload = {
        "origin_label": origin.canonical_label,
        "origin_alias": origin_alias,
        "destination_label": destination.canonical_label,
        "destination_alias": destination_alias,
        "takeoff_latitude": origin.latitude,
        "takeoff_longitude": origin.longitude,
        "dropoff_latitude": destination.latitude,
        "dropoff_longitude": destination.longitude,
        "planned_route_m": round(route_distance_m, 3),
        "operator_requested_wind_speed_mps": requested_wind_speed_mps,
        "operator_requested_wind_direction_deg": requested_wind_direction_deg,
        "operator_requested_wind_gust_mps": requested_wind_gust_mps,
        "operator_requested_wind_variance": requested_wind_variance,
        "operator_requested_temperature_c": requested_temperature_c,
        "operator_requested_pressure_hpa": requested_pressure_hpa,
        "operator_requested_thermal_battery_drain_factor": (
            requested_thermal_battery_drain_factor
        ),
        "operator_requested_thermal_motor_derate_factor": (
            requested_thermal_motor_derate_factor
        ),
        "operator_requested_payload_weight_kg": requested_payload_weight_kg,
        "semantic_route_request_ref": (
            f"missionos_chief_semantic_route_request:{semantic_request['sha256'][:16]}"
            if semantic_request.get("sha256")
            else ""
        ),
    }
    route_result_hash = _content_hash(route_result_payload)
    route_tool = {
        "schema_version": ROUTE_RESOLVER_TOOL_SCHEMA_VERSION,
        "tool_name": "missionos_route_resolver_tool",
        "tool_status": "resolved",
        "provider": (
            "missionos_fixture_place_registry_plus_postal_geocoder_and_nominatim"
            if postal_payloads and geocode_payloads
            else "missionos_fixture_place_registry_plus_postal_geocoder"
            if postal_payloads
            else "missionos_fixture_place_registry_plus_nominatim"
            if geocode_payloads
            else "missionos_fixture_place_registry"
        ),
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "source_backed_route": True,
        "source_url": (
            "fixture://boiled-claw/missionos/chief-place-registry+zipcloud+nominatim"
            if postal_payloads and geocode_payloads
            else "fixture://boiled-claw/missionos/chief-place-registry+zipcloud+nominatim"
            if postal_payloads
            else "fixture://boiled-claw/missionos/chief-place-registry+nominatim"
            if geocode_payloads
            else "fixture://boiled-claw/missionos/chief-place-registry"
        ),
        "source_refs": list(origin.source_refs + destination.source_refs),
        "resolved_at": resolved_at.isoformat(),
        "dispatch_authority_created": False,
        "progress_counted": False,
        "route_hash": route_result_hash,
        "sha256": route_result_hash,
        **route_result_payload,
    }

    weather_url = _open_meteo_jma_url(destination.latitude, destination.longitude)
    weather_status = "source_backed_weather_captured"
    provider = "open_meteo_jma"
    provider_response_status = "not_requested"
    source_unavailable = False
    source_backed_weather = True
    weather_values: dict[str, Any] = {
        "valid_at": None,
        "precipitation_mm_per_hour": None,
        "wind_speed_mps": None,
        "wind_gust_mps": None,
        "wind_direction_deg": None,
        "temperature_c": None,
        "pressure_hpa": None,
    }
    try:
        provider_response_status, payload = _fetch_weather_payload(
            weather_url,
            fetcher=weather_fetcher,
            timeout_seconds=weather_timeout_seconds,
        )
        current = payload.get("current") if isinstance(payload, Mapping) else {}
        if not isinstance(current, Mapping) or not current.get("time"):
            raise ValueError("Open-Meteo response missing current conditions")
        weather_values["valid_at"] = str(current.get("time"))
        weather_values["precipitation_mm_per_hour"] = max(
            0.0,
            _optional_float(current.get("precipitation")) or 0.0,
        )
        wind_speed_kmh = _optional_float(current.get("wind_speed_10m"))
        wind_gust_kmh = _optional_float(current.get("wind_gusts_10m"))
        weather_values["wind_speed_mps"] = (
            round(wind_speed_kmh / 3.6, 3) if wind_speed_kmh is not None else None
        )
        weather_values["wind_gust_mps"] = (
            round(wind_gust_kmh / 3.6, 3) if wind_gust_kmh is not None else None
        )
        weather_values["wind_direction_deg"] = _optional_float(current.get("wind_direction_10m"))
        weather_values["temperature_c"] = _optional_float(current.get("temperature_2m"))
        weather_values["pressure_hpa"] = _optional_float(current.get("surface_pressure"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        weather_status = "blocked_source_unavailable"
        provider = "source_backed_weather_unavailable"
        provider_response_status = f"source_unavailable:{type(exc).__name__}"
        source_unavailable = True
        source_backed_weather = False

    weather_hash_payload = {
        "provider": provider,
        "source_url": weather_url,
        "snapshot_status": weather_status,
        "latitude": destination.latitude,
        "longitude": destination.longitude,
        "provider_response_status": provider_response_status,
        "source_backed_weather": source_backed_weather,
        "source_unavailable": source_unavailable,
        **weather_values,
    }
    weather_hash = _content_hash(weather_hash_payload)
    weather_tool = {
        "schema_version": WEATHER_RESOLVER_TOOL_SCHEMA_VERSION,
        "tool_name": "missionos_weather_resolver_tool",
        "tool_status": weather_status,
        "provider": provider,
        "source_url": weather_url,
        "source_refs": [_route_ref({"route_id": route_result_hash[:12]})],
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "destination_label": destination.canonical_label,
        "latitude": destination.latitude,
        "longitude": destination.longitude,
        "captured_at": resolved_at.isoformat(),
        "provider_response_status": provider_response_status,
        "source_backed_weather": source_backed_weather,
        "source_unavailable": source_unavailable,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "weather_hash": weather_hash,
        "sha256": weather_hash,
        **weather_values,
    }
    terrain_tool, terrain_profile = _resolve_route_terrain_profile(
        origin=origin,
        destination=destination,
        planned_route_m=route_distance_m,
        resolved_at=resolved_at,
        terrain_fetcher=terrain_fetcher,
        timeout_seconds=terrain_timeout_seconds,
    )

    route_id = _stable_id(
        "chief_route",
        {
            "origin": origin.canonical_label,
            "destination": destination.canonical_label,
            "route_hash": route_result_hash,
            "weather_hash": weather_hash,
        },
    )
    coordinate_route: dict[str, Any] = {
        "schema_version": COORDINATE_ROUTE_TOOL_SCHEMA_VERSION,
        "route_id": route_id,
        "route_source": "missionos_chief_internal_route_weather_tools",
        "takeoff_label": origin.canonical_label,
        "dropoff_label": destination.canonical_label,
        "takeoff_latitude": origin.latitude,
        "takeoff_longitude": origin.longitude,
        "dropoff_latitude": destination.latitude,
        "dropoff_longitude": destination.longitude,
        "dropoff_roof_height_agl_m": 30.0,
        "payload_weight_kg": requested_payload_weight_kg if requested_payload_weight_kg is not None else 0.5,
        "planned_route_m": round(route_distance_m, 3),
        "auto_route_waypoint_count": _int_field(
            semantic_request.get("auto_route_waypoint_count")
        )
        or 20,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "source_refs": [
            f"missionos_route_resolver_tool_result:{route_result_hash[:16]}",
            f"missionos_weather_resolver_tool_result:{weather_hash[:16]}",
            f"missionos_terrain_elevation_resolver_tool_result:{terrain_tool['sha256'][:16]}",
            *(
                [f"missionos_chief_semantic_route_request:{semantic_request['sha256'][:16]}"]
                if semantic_request.get("sha256")
                else []
            ),
            *[
                f"missionos_postal_code_resolver_tool_result:{payload['sha256'][:16]}"
                for payload in postal_payloads
                if payload.get("sha256")
            ],
            *[
                f"missionos_place_geocoder_tool_result:{payload['sha256'][:16]}"
                for payload in geocode_payloads
                if payload.get("sha256")
            ],
        ],
    }
    if terrain_profile:
        coordinate_route["terrain_profile"] = [dict(sample) for sample in terrain_profile]
        coordinate_route["terrain_clearance_agl_m"] = DEFAULT_TERRAIN_CLEARANCE_AGL_M
        coordinate_route["terrain_profile_source"] = terrain_tool["provider"]
        terrain_ref = (
            f"missionos_terrain_elevation_resolver_tool_result:{terrain_tool['sha256'][:16]}"
        )
        coordinate_route["terrain_profile_ref"] = terrain_ref
        coordinate_route["source_refs"] = [
            *coordinate_route["source_refs"],
            *([] if terrain_ref in coordinate_route["source_refs"] else [terrain_ref]),
        ]
    _apply_operator_requested_obstacle_flags(coordinate_route, text)
    if requested_wind_speed_mps is not None:
        coordinate_route["wind_speed_mps"] = requested_wind_speed_mps
        coordinate_route["wind_speed_mps_operator_requested"] = requested_wind_speed_mps
        coordinate_route["wind_speed_source"] = "operator_instruction"
    elif weather_tool.get("wind_speed_mps") is not None:
        coordinate_route["wind_speed_mps"] = weather_tool["wind_speed_mps"]
        coordinate_route["wind_speed_source"] = "source_backed_weather"
    if weather_tool.get("source_backed_weather") and weather_tool.get("valid_at"):
        coordinate_route["source_weather_valid_at"] = weather_tool.get("valid_at")
    if requested_wind_direction_deg is not None:
        coordinate_route["wind_direction_deg"] = requested_wind_direction_deg
        coordinate_route["wind_direction_source"] = "operator_instruction"
    elif weather_tool.get("wind_direction_deg") is not None:
        coordinate_route["wind_direction_deg"] = weather_tool["wind_direction_deg"]
        coordinate_route["wind_direction_source"] = "source_backed_weather"
    if requested_wind_gust_mps is not None:
        coordinate_route["wind_gust_mps"] = requested_wind_gust_mps
        coordinate_route["wind_gust_source"] = "operator_instruction"
    elif weather_tool.get("wind_gust_mps") is not None:
        coordinate_route["wind_gust_mps"] = weather_tool["wind_gust_mps"]
        coordinate_route["wind_gust_source"] = "source_backed_weather"
    if requested_wind_variance is not None:
        coordinate_route["wind_variance"] = requested_wind_variance
        coordinate_route["wind_variance_source"] = "operator_instruction"
    if requested_temperature_c is not None:
        coordinate_route["temperature_c"] = requested_temperature_c
        coordinate_route["temperature_source"] = "operator_instruction"
    elif weather_tool.get("temperature_c") is not None:
        coordinate_route["temperature_c"] = weather_tool["temperature_c"]
        coordinate_route["temperature_source"] = "source_backed_weather"
    if requested_pressure_hpa is not None:
        coordinate_route["pressure_hpa"] = requested_pressure_hpa
        coordinate_route["pressure_source"] = "operator_instruction"
    elif weather_tool.get("pressure_hpa") is not None:
        coordinate_route["pressure_hpa"] = weather_tool["pressure_hpa"]
        coordinate_route["pressure_source"] = "source_backed_weather"
    precipitation = _float_field(weather_tool.get("precipitation_mm_per_hour"))
    if precipitation is not None:
        coordinate_route["precipitation_mm_per_hour"] = precipitation
        coordinate_route["precipitation_source"] = "source_backed_weather"
        if precipitation > 0.0:
            coordinate_route["rain_visual_mode"] = "rain"
            coordinate_route["rain_effect_model"] = "bounded_sitl_approximation"
            coordinate_route["rain_battery_drain_factor"] = round(
                min(1.8, 1.0 + precipitation * 0.04),
                3,
            )
            coordinate_route["rain_sensor_degradation_factor"] = round(
                min(0.45, precipitation * 0.035),
                3,
            )
            coordinate_route["rain_landing_risk_factor"] = round(
                min(2.5, 1.0 + precipitation * 0.08),
                3,
            )
            coordinate_route["rain_effect_source"] = "source_backed_weather"
    if requested_thermal_battery_drain_factor is not None:
        coordinate_route["thermal_battery_drain_factor"] = (
            requested_thermal_battery_drain_factor
        )
        coordinate_route["thermal_battery_drain_factor_source"] = (
            "operator_instruction"
        )
    if requested_thermal_motor_derate_factor is not None:
        coordinate_route["thermal_motor_derate_factor"] = (
            requested_thermal_motor_derate_factor
        )
        coordinate_route["thermal_motor_derate_factor_source"] = (
            "operator_instruction"
        )

    status = "resolved" if source_backed_weather else "partial"
    internal_tool_names = [
        "missionos_route_resolver_tool",
        "missionos_weather_resolver_tool",
        "missionos_terrain_elevation_resolver_tool",
    ]
    if semantic_request.get("tool_status") == "resolved":
        internal_tool_names.insert(0, "missionos_chief_semantic_route_request")
    if geocode_payloads:
        internal_tool_names.insert(0, "missionos_place_geocoder_tool")
    if postal_payloads:
        internal_tool_names.insert(0, "missionos_postal_code_resolver_tool")
    result_hash = _content_hash({
        "semantic_route_request": semantic_request,
        "route_tool": route_tool,
        "weather_tool": weather_tool,
        "terrain_tool": terrain_tool,
        "postal_code_resolvers": postal_payloads,
        "place_geocoder_resolvers": geocode_payloads,
        "coordinate_route": coordinate_route,
        "tool_status": status,
    })
    return {
        "schema_version": CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION,
        "tool_status": status,
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "internal_tool_names": internal_tool_names,
        "semantic_route_request": semantic_request,
        "route_resolver": route_tool,
        "postal_code_resolvers": postal_payloads,
        "place_geocoder_resolvers": geocode_payloads,
        "weather_resolver": weather_tool,
        "terrain_resolver": terrain_tool,
        "coordinate_route": coordinate_route,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "resolved_at": resolved_at.isoformat(),
        "planner_tools_hash": result_hash,
        "sha256": result_hash,
    }


def extract_operator_requested_route_overrides(utterance: str) -> dict[str, Any]:
    """Extract parameter-only follow-up changes for an existing Chief route."""
    text = str(utterance or "").strip()
    overrides: dict[str, Any] = {}
    requested_wind_speed_mps = _operator_requested_wind_speed_mps(text)
    if requested_wind_speed_mps is not None:
        overrides["wind_speed_mps"] = requested_wind_speed_mps
        overrides["wind_speed_mps_operator_requested"] = requested_wind_speed_mps
        overrides["wind_speed_source"] = "operator_followup_instruction"
    requested_payload_weight_kg = _operator_requested_payload_weight_kg(text)
    if requested_payload_weight_kg is not None:
        overrides["payload_weight_kg"] = requested_payload_weight_kg
        overrides["payload_weight_kg_operator_requested"] = requested_payload_weight_kg
        overrides["payload_weight_source"] = "operator_followup_instruction"
    obstacle_flags = _operator_requested_obstacle_flags(text)
    if obstacle_flags:
        overrides.update(obstacle_flags)
    return overrides


__all__ = [
    "CHIEF_PLANNER_INTERNAL_TOOLS_SCHEMA_VERSION",
    "COORDINATE_ROUTE_TOOL_SCHEMA_VERSION",
    "ROUTE_RESOLVER_TOOL_SCHEMA_VERSION",
    "SEMANTIC_ROUTE_REQUEST_SCHEMA_VERSION",
    "WEATHER_RESOLVER_TOOL_SCHEMA_VERSION",
    "extract_operator_requested_route_overrides",
    "resolve_chief_planner_internal_tools",
    "resolve_chief_semantic_route_request",
]
