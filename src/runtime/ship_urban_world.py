"""Approved urban route alternatives and their collision-enabled world geometry."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from src.runtime.ship_delivery_world import _box
from src.runtime.ship_urban_decision import CASES, POLICIES, DETOUR_X_M

STATIC_CASES = {"static_center": 0.0, "static_near": 8.0, "static_clear": 24.0}


def configure_urban(scenario, case, policy):
    from src.runtime.ship_onboard import POLICIES as ONBOARD_POLICIES

    onboard = policy in ONBOARD_POLICIES
    if (
        case not in CASES and not (onboard and case in ("brake_stop", *STATIC_CASES))
    ) or policy not in (
        *POLICIES,
        *ONBOARD_POLICIES,
    ):
        raise ValueError("Unknown urban case or policy")
    if scenario.offshore_distance_m < 80 or scenario.urban_distance_m < 200:
        raise ValueError("Urban screen requires offshore >= 80m and urban >= 200m")
    if not 25 <= scenario.cruise_altitude_m <= 35:
        raise ValueError("Urban corridor cruise altitude must be 25–35m")
    return {
        "case": case,
        "policy": policy,
        **({"stationary_obstacle_east_m": STATIC_CASES[case]} if case in STATIC_CASES else {}),
        "scripted_obstacle_speed_mps": CASES.get(case, 4.0),
        "entry_north_m": scenario.offshore_distance_m - 30,
        "obstacle_north_m": scenario.offshore_distance_m + 70,
        "join_north_m": scenario.offshore_distance_m + 130,
        "cruise_altitude_m": scenario.cruise_altitude_m,
        "airspeed_mps": scenario.airspeed_mps,
        "obstacle_size_m": [16, 8, 60],
        "policy_observation_source": "onboard_rgb_px4_ego_mapped_plane"
        if onboard
        else "gazebo_pose_history",
        "onboard_camera": onboard,
        "learned_model_comparison_admitted": False,
    }


def urbanize_world(model_root, urban):
    path = model_root / "worlds/default.sdf"
    tree = ET.parse(path)
    world = tree.getroot().find("world")
    buildings = []
    for model in world.findall("model"):
        if not model.get("name", "").startswith("urban_building_"):
            continue
        point = [float(v) for v in model.findtext("pose").split()[:3]]
        point[2] = 22.5
        name = model.get("name")
        world.remove(model)
        world.append(
            ET.fromstring(
                _box(name, point, (35, 30, 45), "0.6 0.6 0.65 1", contact_topic="contacts")
            )
        )
        buildings.append({"name": name, "xyz": point, "size": [35, 30, 45]})
    world.append(
        ET.fromstring(
            _box(
                "urban_obstacle",
                (urban.get("stationary_obstacle_east_m", 0), urban["obstacle_north_m"], 30),
                (16, 8, 60),
                "0.9 0.25 0.1 1",
                contact_topic="contacts",
            )
        )
    )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    urban["buildings"] = buildings
    urban["collision_entities"] = ["urban_obstacle", *[b["name"] for b in buildings]]


def urbanize_missions(missions, urban):
    template = missions["outbound"][0]
    alt = urban["cruise_altitude_m"]

    def item(x, y, *, command=16, altitude=None):
        return dict(
            template,
            command=command,
            latitude_deg=round(35.3195 + math.degrees(y / 6371000), 7),
            longitude_deg=round(
                138.7435 + math.degrees(x / (6371000 * math.cos(math.radians(35.3195)))), 7
            ),
            altitude_m=alt if altitude is None else altitude,
            param1=0.0,
            param2=0.0,
        )

    entry, join, goal = urban["entry_north_m"], urban["join_north_m"], missions["goal_north_m"]
    missions["outbound"] = [
        item(0, 0, command=22),
        *[item(0, y) for y in range(100, int(entry), 100)],
        item(0, entry, command=17),
    ]
    common = [item(0, join), item(0, goal), item(0, goal, command=17, altitude=3)]
    missions["urban-wait"] = [dict(i) for i in common]
    missions["urban-detour"] = [
        item(DETOUR_X_M, entry),
        item(DETOUR_X_M, join),
        *[dict(i) for i in common],
    ]
    # A common preapproved return detour keeps the moving obstacle out of the
    # return path for every policy. Compare the outbound urban interval only.
    missions["return"] = [
        item(0, goal),
        item(0, join),
        item(DETOUR_X_M, join),
        item(DETOUR_X_M, entry),
        item(0, entry),
        *[item(0, y) for y in reversed(range(100, int(entry), 100))],
        item(0, 0),
        item(0, 0, command=21, altitude=0),
    ]
    for name in ("outbound", "urban-wait", "urban-detour", "return"):
        for seq, value in enumerate(missions[name]):
            value.update(seq=seq, current=int(seq == 0))


def verify_urban_run(samples, events, urban):
    reasons = []
    by_name = {e["event"]: e for e in events}
    required = [
        "urban_entry_observed",
        "urban_actor_started",
        "urban_decision",
        "urban_upload",
        "urban_ready",
        "urban_requested",
        "delivery_hover_observed",
    ]
    if not all(name in by_name for name in required):
        return {"verified": False, "reasons": ["urban_events_missing"]}
    if [by_name[n]["elapsed_s"] for n in required] != sorted(
        by_name[n]["elapsed_s"] for n in required
    ):
        reasons.append("urban_event_order_invalid")
    from src.runtime.ship_urban_decision import choose_urban_action

    decision = by_name["urban_decision"]
    if urban.get("onboard_camera"):
        from src.runtime.ship_onboard import choose_onboard_action

        from src.runtime.ship_onboard_model import MODEL_POLICIES, interpret_proposal

        if urban["policy"] == "onboard_anwm_static":
            from src.runtime.ship_anwm_static import interpret

            stored = decision["decision"]
            expected = interpret(
                stored["anwm"]["predictions"],
                stored["anwm"]["revalidated_obstacle_x_m"],
                stored["inference_elapsed_s"],
            )
            expected["anwm"].update(
                {
                    k: stored["anwm"][k]
                    for k in (
                        "response_sha256",
                        "request_id",
                        "monitored_samples",
                        "latest_observed_at_s",
                    )
                }
            )
        elif urban["policy"] in MODEL_POLICIES:
            expected = interpret_proposal(
                decision["decision"]["model_proposal"],
                urban["policy"],
                elapsed_s=decision["decision"]["inference_elapsed_s"],
            )
        else:
            expected = choose_onboard_action(
                decision["history"], urban["policy"], airspeed_mps=urban["airspeed_mps"]
            )
    else:
        expected = choose_urban_action(
            decision["history"], urban["policy"], airspeed_mps=urban["airspeed_mps"]
        )
    if decision["decision"] != expected:
        reasons.append("urban_decision_not_reproducible")
    if by_name["urban_requested"].get("action") != expected["action"]:
        reasons.append("urban_dispatch_action_mismatch")
    for observation in decision["history"]:
        matching = [
            r
            for r in samples
            if r["elapsed_s"] == observation["observed_at_s"] and r.get("urban_obstacle")
        ]
        if (
            not matching
            or (
                matching[0].get("onboard_frame", {}).get("sha256") != observation["image_sha256"]
                if urban.get("onboard_camera")
                else matching[0]["urban_obstacle"]["xyz"][0] != observation["obstacle_x_m"]
            )
            or observation["observed_at_s"] > decision["elapsed_s"]
        ):
            reasons.append("urban_policy_input_not_bound_to_observation")
    rows = [s for s in samples if s["elapsed_s"] >= by_name["urban_actor_started"]["elapsed_s"]]

    def valid_position(entity):
        if not isinstance(entity, dict):
            return False
        xyz = entity.get("xyz")
        return (
            isinstance(xyz, list)
            and len(xyz) == 3
            and all(type(v) in (int, float) and math.isfinite(v) for v in xyz)
        )

    if not rows or any(
        not r.get("poses_fresh")
        or not valid_position(r.get("vehicle"))
        or not valid_position(r.get("urban_obstacle"))
        or not r.get("urban_contact_monitors_connected")
        for r in rows
    ):
        return {"verified": False, "reasons": [*reasons, "urban_observation_missing_or_stale"]}
    if any(r.get("urban_contact_observed") for r in samples):
        reasons.append("urban_collision_observed")
    # Event-only contact streams have no empty-contact heartbeat. Independently
    # test swept sample segments against inflated collision boxes.
    for before, after in zip(rows, rows[1:]):
        if not all(r.get("vehicle") and r.get("urban_obstacle") for r in (before, after)):
            continue
        boxes = [(before["urban_obstacle"]["xyz"], after["urban_obstacle"]["xyz"], [16, 8, 60])]
        boxes.extend((b["xyz"], b["xyz"], b["size"]) for b in urban["buildings"])
        if any(
            segment_intersects_box(
                [v - c for v, c in zip(before["vehicle"]["xyz"], center_a)],
                [v - c for v, c in zip(after["vehicle"]["xyz"], center_b)],
                [dimension / 2 + 0.75 for dimension in size],
            )
            for center_a, center_b, size in boxes
        ):
            reasons.append("urban_geometric_clearance_violated")
            break
    for row in rows:
        buildings = row.get("urban_buildings", [])
        if len(buildings) != len(urban["buildings"]) or any(
            not valid_position(p) or math.dist(p["xyz"], b["xyz"]) > 0.01
            for p, b in zip(buildings, urban["buildings"])
        ):
            reasons.append("urban_buildings_not_observed")
            break
    points = [r["urban_obstacle"] for r in rows if r.get("urban_obstacle")]
    if (
        not points
        or len({p["id"] for p in points}) != 1
        or (
            max(abs(p["xyz"][0] - urban["stationary_obstacle_east_m"]) for p in points) > 0.01
            if "stationary_obstacle_east_m" in urban
            else max(p["xyz"][0] for p in points) - min(p["xyz"][0] for p in points) < 1
        )
    ):
        reasons.append("same_moving_obstacle_not_observed")
    start, end = (
        by_name["urban_entry_observed"]["elapsed_s"],
        by_name["delivery_hover_observed"]["elapsed_s"],
    )
    segment = [r for r in samples if start <= r["elapsed_s"] <= end and r.get("vehicle")]
    lateral = max((abs(r["vehicle"]["xyz"][0]) for r in segment), default=0)
    if expected["action"] == "detour" and lateral < 75:
        reasons.append("detour_motion_not_observed")
    if expected["action"] == "wait":
        dispatch = by_name["urban_requested"]["elapsed_s"]
        preceding = [r for r in rows if r["elapsed_s"] <= dispatch and r.get("urban_obstacle")]
        if not preceding or preceding[-1]["urban_obstacle"]["xyz"][0] < 13:
            reasons.append("direct_departure_before_observed_clearance")
    return {
        "verified": not reasons,
        "reasons": reasons,
        "action": expected["action"],
        "policy": urban["policy"],
        "urban_elapsed_s": end - start,
        "max_outbound_lateral_m": lateral,
        "collision_observed": any(r.get("urban_contact_observed") for r in samples),
        "geometric_clearance_verified": "urban_geometric_clearance_violated" not in reasons
        and bool(rows),
        "observation_source": urban["policy_observation_source"],
        "learned_model_comparison_admitted": False,
    }


def segment_intersects_box(start, end, half_size):
    lower, upper = 0.0, 1.0
    for a, b, half in zip(start, end, half_size):
        velocity = b - a
        if abs(velocity) < 1e-10:
            if abs(a) > half:
                return False
            continue
        entry, leave = sorted(((-half - a) / velocity, (half - a) / velocity))
        lower, upper = max(lower, entry), min(upper, leave)
        if lower > upper:
            return False
    return True
