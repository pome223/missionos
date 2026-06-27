#!/usr/bin/env python3
"""Render a read-only operator report from a Digital Twin smoke summary."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
    SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS,
    SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
)

OPERATOR_REVIEW_NOTE = (
    "Mission OS records delivery evidence but does not claim delivery completion. "
    "The operator must review this report and approve completion externally if appropriate."
)


def _bool_text(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _value_text(value: Any) -> str:
    if value is None or value == "":
        return "not observed"
    if isinstance(value, bool):
        return _bool_text(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def _position_text(summary: Mapping[str, Any], prefix: str) -> str:
    x = summary.get(f"{prefix}_x_m")
    y = summary.get(f"{prefix}_y_m")
    z = summary.get(f"{prefix}_z_m")
    if x is None or y is None:
        return "not observed"
    if z is None:
        return f"x={x}, y={y}"
    return f"x={x}, y={y}, z={z}"


def _route_distance_label(summary: Mapping[str, Any]) -> str:
    distance = summary.get("route_distance_m")
    if not isinstance(distance, (int, float)):
        return "configured-distance"
    if distance >= 1000 and distance % 1000 == 0:
        return f"{distance / 1000:g}km"
    if distance >= 1000:
        return f"{distance / 1000:.1f}km"
    return f"{distance:g}m"


def _observed_flow_lines(summary: Mapping[str, Any]) -> list[str]:
    flow: list[str] = []

    if summary.get("takeoff_observed") is False:
        flow.append("ドローンの takeoff は観測されていない")
    else:
        flow.append("ドローンが飛んだ")

    route_label = _route_distance_label(summary)
    if summary.get("mountain_hut_waypoint_reached") is True:
        flow.append(f"{route_label} 先の waypoint に到達した")
    elif summary.get("mountain_hut_waypoint_reached") is False:
        flow.append(f"{route_label} 先の waypoint には未到達")
    else:
        flow.append(f"{route_label} 先の waypoint 到達は未確認")

    payload_source = summary.get("payload_release_event_source")
    if (
        summary.get("payload_release_observed") is True
        and payload_source == "gazebo_detachable_joint_detach_event"
    ):
        flow.append("payload release が Gazebo detachable-joint event として観測された")
    elif summary.get("payload_release_observed") is True:
        flow.append(f"payload release が {_value_text(payload_source)} として観測された")
    elif summary.get("payload_release_observed") is False:
        flow.append("payload release は観測されていない")
    else:
        flow.append("payload release 観測は未確認")

    if summary.get("dropoff_verified") is True:
        flow.append("既存 dropoff verifier が「投下位置は許容内」と判定した")
    elif summary.get("dropoff_verification_applied") is True:
        status = _value_text(summary.get("dropoff_verification_status"))
        flow.append(f"既存 dropoff verifier は適用されたが、判定は {status}")
    else:
        flow.append("既存 dropoff verifier はまだ適用されていない")

    if summary.get("landing_observed") is True:
        flow.append("着陸も観測された")
    elif summary.get("landing_observed") is False:
        flow.append("着陸は観測されていない")
    else:
        flow.append("着陸観測は未確認")

    if summary.get("gcs_link_loss_observed") is False and summary.get("rtl_observed") is False:
        flow.append("通信断や RTL は起きていない")
    else:
        flow.append(
            "通信断 / RTL 観測: "
            f"gcs_link_loss={_bool_text(summary.get('gcs_link_loss_observed'))}, "
            f"rtl={_bool_text(summary.get('rtl_observed'))}"
        )
    return flow


def render_delivery_evidence_report(summary: Mapping[str, Any]) -> str:
    """Render existing evidence as non-authoritative Markdown."""

    dropoff = summary.get("dropoff_verification_summary")
    if not isinstance(dropoff, Mapping):
        dropoff = {}
    blocked_reasons = summary.get("blocked_reasons") or ()
    dropoff_blocked = dropoff.get("dropoff_verification_blocked_reasons") or ()
    lines = [
        "# Delivery Evidence Report",
        "",
        "## What Happened",
        "",
        *[line for item in _observed_flow_lines(summary) for line in (item, "↓")][:-1],
        "",
        "## Observed Evidence",
        "",
        f"- route scope: {_value_text(summary.get('route_mode'))} ({_value_text(summary.get('route_distance_m'))} m)",
        f"- weather scenario: {_value_text(summary.get('weather_scenario'))}",
        f"- source weather snapshot: {_value_text(summary.get('source_weather_wind_snapshot_ref'))}",
        f"- source weather status: {_value_text(summary.get('source_weather_wind_snapshot_status'))}",
        f"- source weather provider: {_value_text(summary.get('source_weather_wind_provider'))}",
        f"- source weather fetch mode: {_value_text(summary.get('source_weather_fetch_mode'))}",
        f"- wind scenario enabled: {_bool_text(summary.get('wind_scenario_enabled'))}",
        f"- wind speed: {_value_text(summary.get('wind_speed_mps'))} m/s",
        f"- wind direction: {_value_text(summary.get('wind_direction_deg'))} deg",
        f"- waypoint reached: {_bool_text(summary.get('mountain_hut_waypoint_reached'))}",
        f"- mission item reached seq: {_value_text(summary.get('mission_item_reached_seq'))}",
        f"- payload release observed: {_bool_text(summary.get('payload_release_observed'))}",
        f"- payload release source: {_value_text(summary.get('payload_release_event_source'))}",
        f"- dropoff verifier applied: {_bool_text(summary.get('dropoff_verification_applied'))}",
        f"- dropoff verification status: {_value_text(summary.get('dropoff_verification_status'))}",
        f"- dropoff verified: {_bool_text(summary.get('dropoff_verified'))}",
        f"- landing observed: {_bool_text(summary.get('landing_observed'))}",
        f"- GCS link loss observed: {_bool_text(summary.get('gcs_link_loss_observed'))}",
        f"- RTL observed: {_bool_text(summary.get('rtl_observed'))}",
        f"- blocked reasons: {_value_text(blocked_reasons)}",
        f"- dropoff blocked reasons: {_value_text(dropoff_blocked)}",
        f"- delivery_completion_claimed: {_bool_text(summary.get('delivery_completion_claimed'))}",
        "",
        "## Verifier Semantics",
        "",
        "- coordinate frame: Gazebo local/world frame",
        "- dropoff waypoint seq: 1",
        "- LAND seq: 2 (landing only, not dropoff authority)",
        f"- radius tolerance: {SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M} m",
        f"- altitude tolerance: {SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M} m",
        f"- release time window: {SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS} s",
        f"- payload release position: {_position_text(summary, 'payload_release_position')}",
        f"- dropoff target position: {_position_text(summary, 'dropoff_target')}",
        f"- observed distance to dropoff: {_value_text(dropoff.get('observed_distance_to_dropoff_m'))} m",
        f"- observed altitude error: {_value_text(dropoff.get('observed_altitude_error_m'))} m",
        "",
        "## Operator Review",
        "",
        OPERATOR_REVIEW_NOTE,
        "",
        "Based on the evidence above, the operator may choose whether to approve this simulated delivery as completed.",
    ]
    return "\n".join(lines) + "\n"


def _load_summary(text: str) -> dict[str, Any]:
    stripped = text.strip()
    for line in stripped.splitlines():
        if line.startswith("SMOKE_SUMMARY_JSON "):
            return json.loads(line.removeprefix("SMOKE_SUMMARY_JSON "))
    return json.loads(stripped)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a non-authoritative operator delivery evidence report.",
    )
    parser.add_argument(
        "summary_json",
        nargs="?",
        help="Path to a Digital Twin smoke summary JSON file. Reads stdin when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.summary_json:
        text = Path(args.summary_json).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    print(render_delivery_evidence_report(_load_summary(text)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
