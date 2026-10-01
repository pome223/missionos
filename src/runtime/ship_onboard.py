"""Onboard image observation and matched rules for the bounded urban experiment.

Known colour and a mapped obstacle plane are explicit synthetic assumptions.
Gazebo obstacle poses and case names never enter the image selector.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

from src.runtime.ship_urban_decision import choose_urban_action
from src.runtime.ship_onboard_model import MODEL_POLICIES


RULE_POLICIES = (
    "onboard_wait",
    "onboard_detour",
    "onboard_velocity",
    "onboard_stopping",
    "onboard_uncertainty",
)
POLICIES = (*RULE_POLICIES, *MODEL_POLICIES, "onboard_anwm_static")
FX = 640 / (2 * math.tan(math.pi / 6))
CAMERA_YAW_CORRECTION_LIMIT_RAD = 0.15


def add_onboard_camera(model_root, urban):
    path = model_root / "x500_base/model.sdf"
    tree = ET.parse(path)
    link = tree.getroot().find("model/link[@name='base_link']")
    if link is None:
        raise ValueError("x500 base_link is missing")
    link.append(
        ET.fromstring("""<sensor name="mission_rgb" type="camera">
      <pose>0.25 0 0.1 0 0 0</pose><always_on>true</always_on><update_rate>10</update_rate>
      <topic>/ship/onboard/rgb</topic><camera><horizontal_fov>1.0471975511965976</horizontal_fov>
      <image><width>640</width><height>360</height><format>R8G8B8</format></image>
      <clip><near>0.1</near><far>500</far></clip></camera></sensor>""")
    )
    if urban.get("capture_anwm"):
        link.append(
            ET.fromstring("""<sensor name="mission_rgbd" type="rgbd_camera">
          <pose>0.25 0 0.1 0 0 0</pose><always_on>true</always_on><update_rate>4</update_rate>
          <topic>/ship/anwm</topic><camera><horizontal_fov>1.0471975511965976</horizontal_fov>
          <image><width>640</width><height>360</height><format>R8G8B8</format></image>
          <clip><near>0.1</near><far>500</far></clip>
          <depth_camera><clip><near>0.1</near><far>500</far></clip></depth_camera>
          </camera></sensor>""")
        )
    if urban.get("capture_anwm") or urban.get("aerovla_live"):
        # The common 20 Hz grid preserves exact 10 Hz VLA and 4 Hz RGBD joins.
        down_rate = (
            20
            if urban.get("capture_anwm") and urban.get("vla_executor_contract")
            else (10 if urban.get("aerovla_live") else 4)
        )
        link.append(
            ET.fromstring(f"""<sensor name="mission_down" type="camera">
          <pose>0.25 0 0.1 0 1.5707963267948966 0</pose><always_on>true</always_on><update_rate>{down_rate}</update_rate>
          <topic>/ship/aerovla/down</topic><camera><horizontal_fov>1.0471975511965976</horizontal_fov>
          <image><width>640</width><height>360</height><format>R8G8B8</format></image>
          <clip><near>0.1</near><far>500</far></clip></camera></sensor>""")
        )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    if urban.get("capture_anwm"):
        vehicle_path = model_root / "x500/model.sdf"
        vehicle_tree = ET.parse(vehicle_path)
        vehicle_tree.getroot().find("model").append(
            ET.fromstring("""<plugin
          filename="gz-sim-pose-publisher-system" name="gz::sim::systems::PosePublisher">
          <publish_model_pose>true</publish_model_pose><publish_link_pose>false</publish_link_pose>
          <publish_nested_model_pose>false</publish_nested_model_pose><use_pose_vector_msg>true</use_pose_vector_msg>
          <update_frequency>-1</update_frequency></plugin>""")
        )
        vehicle_tree.write(vehicle_path, encoding="utf-8", xml_declaration=True)
    world_path = model_root / "worlds/default.sdf"
    world_tree = ET.parse(world_path)
    world = world_tree.getroot().find("world")
    # A surveyed, non-colliding visual fiducial corrects PX4 yaw-estimator bias.
    # It is static map information shared by every selector, not obstacle truth.
    north = urban["obstacle_north_m"] - 5
    world.append(
        ET.fromstring(f"""<model name="surveyed_camera_marker"><static>true</static>
      <pose>-14 {north} 30 0 0 0</pose><link name="link"><visual name="marker">
      <geometry><box><size>2 0.2 4</size></box></geometry><material>
      <ambient>0.05 0.85 0.95 1</ambient><diffuse>0.05 0.85 0.95 1</diffuse>
      </material></visual></link></model>""")
    )
    if not any("Sensors" in p.get("name", "") for p in world.findall("plugin")):
        world.append(
            ET.fromstring("""<plugin filename="gz-sim-sensors-system"
          name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>""")
        )
    world_tree.write(world_path, encoding="utf-8", xml_declaration=True)


def rotation(q):
    if len(q) != 4 or not all(type(x) in (int, float) and math.isfinite(x) for x in q):
        raise ValueError("Invalid PX4 attitude")
    if abs(sum(x * x for x in q) - 1) > 0.01:
        raise ValueError("PX4 attitude is not a unit quaternion")
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def observed_obstacle_pixel_center(pixels):
    """Use a visible horizontal cross-section, retaining its vertical bearing.

    A foreground building can cover the lower part of this mapped red box.
    Prefer the widest contiguous band; use the central band to break ties.
    This remains restricted synthetic perception, not general occlusion recovery.
    """
    red = (
        (pixels[:, :, 0] > 80)
        & (pixels[:, :, 0] > 1.6 * pixels[:, :, 1])
        & (pixels[:, :, 0] > 1.6 * pixels[:, :, 2])
    )
    choices = []
    for v in (180, *range(40, 321, 8)):
        columns = np.where(red[v - 2 : v + 3].sum(axis=0) >= 3)[0]
        if (
            len(columns) >= 20
            and columns[0] > 0
            and columns[-1] < 639
            and len(columns) / (columns[-1] - columns[0] + 1) >= 0.9
        ):
            choices.append((len(columns), -abs(v - 179.5), v, float(columns[0] + columns[-1]) / 2))
    if not choices:
        raise ValueError("Mapped red obstacle has no observable cross-section")
    _, _, v, u = max(choices)
    return u, float(v)


def observe_image(root, row, plane_north_m):
    frame = row["onboard_frame"]
    if not isinstance(frame, dict) or not 0 <= frame["received_age_s"] <= 0.6:
        raise ValueError("Stale onboard image")
    filename = frame["file"]
    if (
        not isinstance(filename, str)
        or not filename.startswith("rgb/")
        or Path(filename).parts != ("rgb", Path(filename).name)
    ):
        raise ValueError("Invalid image path")
    path = root / filename
    if (
        (root / "rgb").is_symlink()
        or path.is_symlink()
        or hashlib.sha256(path.read_bytes()).hexdigest() != frame["sha256"]
    ):
        raise ValueError("Image hash mismatch")
    with Image.open(path) as im:
        if im.size != (640, 360) or im.mode != "RGB":
            raise ValueError("Unexpected camera format")
        pixels = np.asarray(im, dtype=float)
    u, v = observed_obstacle_pixel_center(pixels)
    matrix = rotation(row["attitude_q"])
    position = row["local_ned"]
    if len(position) != 3 or not all(
        type(x) in (int, float) and math.isfinite(x) for x in position
    ):
        raise ValueError("Invalid PX4 position")
    origin = np.array(position) + matrix @ np.array([0.25, 0, -0.1])
    cyan = (
        (pixels[:, :, 1] > 80)
        & (pixels[:, :, 2] > 80)
        & (pixels[:, :, 0] < 0.6 * pixels[:, :, 1])
        & (pixels[:, :, 0] < 0.6 * pixels[:, :, 2])
    )
    ys, xs = np.where(cyan)
    if len(xs) < 80 or min(xs) == 0 or max(xs) == 639 or min(ys) == 0 or max(ys) == 359:
        raise ValueError("Surveyed camera marker is not fully observable")
    marker_u, marker_v = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    marker_ray = matrix @ np.array([1, (marker_u - 319.5) / FX, (marker_v - 179.5) / FX])
    marker_vector = np.array([plane_north_m - 1, -14, -30]) - origin
    bias = math.atan2(marker_vector[1], marker_vector[0]) - math.atan2(marker_ray[1], marker_ray[0])
    if abs(bias) > CAMERA_YAW_CORRECTION_LIMIT_RAD:
        raise ValueError("Image/PX4 heading mismatch exceeds the calibration envelope")
    c, s = math.cos(bias), math.sin(bias)
    matrix = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ matrix
    origin = np.array(position) + matrix @ np.array([0.25, 0, -0.1])
    ray = matrix @ np.array([1.0, (u - 319.5) / FX, (v - 179.5) / FX])
    if ray[0] <= 0.8 or not 60 <= plane_north_m - origin[0] <= 120:
        raise ValueError("Camera is outside the calibrated entry envelope")
    east = origin[1] + ray[1] * (plane_north_m - origin[0]) / ray[0]
    return {
        "observed_at_s": row["elapsed_s"],
        "obstacle_x_m": float(east),
        "center_x_px": u,
        "center_y_px": v,
        "image_sha256": frame["sha256"],
        "sensor_stamp_s": frame["sensor_stamp_s"],
        "marker_heading_correction_rad": bias,
    }


def choose_onboard_action(history, policy, *, airspeed_mps=12.0):
    if policy not in RULE_POLICIES:
        raise ValueError("Unknown onboard policy")
    points = [{k: r[k] for k in ("observed_at_s", "obstacle_x_m")} for r in history]
    if policy == "onboard_uncertainty":
        from src.runtime.ship_onboard_uncertainty import choose_initial_gate

        baseline = choose_onboard_action(history, "onboard_stopping", airspeed_mps=airspeed_mps)
        return choose_initial_gate(points, baseline, airspeed_mps=airspeed_mps)
    base = {"onboard_wait": "always_wait", "onboard_detour": "always_detour"}.get(
        policy, "constant_velocity"
    )
    decision = choose_urban_action(points, base, airspeed_mps=airspeed_mps)
    velocity = decision["estimated_obstacle_vx_mps"]
    remaining = max(0, 14 - points[-1]["obstacle_x_m"])
    wait_s = remaining / velocity if velocity > 0.01 else (0.0 if not remaining else None)
    decision["estimated_remaining_wait_s"] = wait_s
    if base == "constant_velocity":
        decision["action"] = (
            "wait"
            if wait_s is not None and wait_s <= decision["estimated_extra_detour_s"]
            else "detour"
        )
    if policy == "onboard_stopping":
        middle = len(points) // 2
        a, b, c = points[0], points[middle], points[-1]
        dt1, dt2 = b["observed_at_s"] - a["observed_at_s"], c["observed_at_s"] - b["observed_at_s"]
        v1 = (b["obstacle_x_m"] - a["obstacle_x_m"]) / dt1
        v2 = (c["obstacle_x_m"] - b["obstacle_x_m"]) / dt2
        acceleration = 2 * (v2 - v1) / (dt1 + dt2)
        decision["estimated_acceleration_mps2"] = acceleration
        # Secants estimate midpoint velocity; project it to the latest frame.
        current_velocity = v2 + acceleration * dt2 / 2
        decision["estimated_current_vx_mps"] = current_velocity
        if (
            acceleration < -0.4
            and c["obstacle_x_m"] + max(0, current_velocity) ** 2 / (-2 * acceleration) < 14
        ):
            decision["action"] = "detour"
    return {**decision, "policy": policy, "observation_source": "onboard_rgb_px4_ego_mapped_plane"}


def process_vision_request(root, config, *, anwm_url=None):
    path = root / "vision-request.json"
    if not path.exists():
        return
    payload = path.read_bytes()
    request = json.loads(payload)
    number = request["request_id"]
    if type(number) is not int or not 1 <= number <= 1000:
        raise ValueError("Invalid vision request id")
    destination = root / f"vision-response-{number}.json"
    if destination.exists():
        return
    response = {"request_sha256": hashlib.sha256(payload).hexdigest(), "request_id": number}
    try:
        if any(request.get(k) != config[k] for k in ("run_id", "world_sha256", "plan_sha256")):
            raise ValueError("Vision request binding mismatch")
        frames = request["frames"]
        if not 1 <= len(frames) <= 12:
            raise ValueError("Invalid image count")
        history = [observe_image(root, r, config["urban"]["obstacle_north_m"] - 4) for r in frames]
        if any(b["sensor_stamp_s"] <= a["sensor_stamp_s"] for a, b in zip(history, history[1:])):
            raise ValueError("Repeated or reversed camera timestamps")
        response["history"] = history
        if request["purpose"] == "decision":
            choose_onboard_action(history, "onboard_velocity", airspeed_mps=config["airspeed_mps"])
            if config["urban"]["policy"] == "onboard_anwm_static":
                from src.runtime.ship_anwm_static import propose

                response["decision"] = propose(root, config, frames, history, anwm_url)
            elif config["urban"]["policy"] in MODEL_POLICIES:
                from src.runtime.ship_onboard_model import propose

                response["decision"] = propose(root, frames, history, config["urban"]["policy"])
            else:
                response["decision"] = choose_onboard_action(
                    history, config["urban"]["policy"], airspeed_mps=config["airspeed_mps"]
                )
        elif request["purpose"] != "clearance" or len(frames) != 1:
            raise ValueError("Unknown vision request purpose")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        response["error"] = str(exc)
    (root / f"vision-request-{number}.json").write_bytes(payload)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(response, indent=2))
    temporary.replace(destination)


def verify_onboard_artifacts(root, config, samples, events):
    """Reopen every decision/clearance image and bind ego state to raw telemetry."""
    reasons, errors = [], []
    anwm_verification = None
    decision_seen = False
    by_time = {r["elapsed_s"]: r for r in samples}
    sources = config["urban"].get("onboard_source_sha256", {})
    if not sources:
        reasons.append("onboard_source_manifest_missing")
    for filename, digest in sources.items():
        if Path(filename).name != filename:
            raise ValueError("Invalid source artifact name")
        if hashlib.sha256((root / "sources" / filename).read_bytes()).hexdigest() != digest:
            reasons.append("onboard_source_artifact_hash_mismatch")
    # A different local analyser cannot silently reinterpret a frozen run.
    for name in ("ship_onboard.py", "ship_onboard_model.py", "ship_onboard_uncertainty.py"):
        if sources and hashlib.sha256(
            Path(__file__).with_name(name).read_bytes()
        ).hexdigest() != sources.get(name):
            reasons.append("onboard_analysis_source_changed")
    for path in sorted(root.glob("vision-request-*.json")):
        request = json.loads(path.read_text())
        number = request["request_id"]
        response = json.loads((root / f"vision-response-{number}.json").read_text())
        if any(request.get(k) != config[k] for k in ("run_id", "world_sha256", "plan_sha256")):
            reasons.append("onboard_request_binding_mismatch")
        if response.get("request_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            reasons.append("onboard_response_binding_mismatch")
        if response.get("error"):
            reasons.append("onboard_processing_error")
            continue
        history = []
        for frame in request["frames"]:
            row = by_time.get(frame["elapsed_s"])
            if row is None or any(row.get(k) != v for k, v in frame.items()):
                reasons.append("onboard_ego_not_bound_to_telemetry")
                continue
            import re

            attitude = re.search(r"\bq:\s*\[([^]]+)\]", row["raw_px4"]["vehicle_attitude"])
            if (
                not attitude
                or [float(x) for x in attitude.group(1).split(",")] != row["attitude_q"]
            ):
                reasons.append("onboard_attitude_not_bound_to_px4")
            raw_position = row["raw_px4"]["vehicle_local_position"]
            parsed_position = []
            for key in "xyz":
                match = re.search(r"\b" + key + r":\s*([-+0-9.eE]+)\b", raw_position)
                parsed_position.append(float(match.group(1)) if match else None)
            if parsed_position != row["local_ned"]:
                reasons.append("onboard_position_not_bound_to_px4")
            observation = observe_image(root, frame, config["urban"]["obstacle_north_m"] - 4)
            history.append(observation)
            if abs(observation["sensor_stamp_s"] - row.get("poses_sensor_stamp_s", -100)) > 0.3:
                reasons.append("onboard_validation_pose_and_image_not_aligned")
            # Truth is validation-only and never enters a selector request.
            errors.append(abs(observation["obstacle_x_m"] - row["urban_obstacle"]["xyz"][0]))
        if response.get("history") != history:
            reasons.append("onboard_image_observation_not_reproducible")
        if request["purpose"] == "decision":
            decision_seen = True
            if config["urban"]["policy"] == "onboard_anwm_static":
                from src.runtime.ship_anwm_static import verify_artifacts

                anwm_verification = verify_artifacts(
                    root, config, response["decision"], samples, events
                )
                expected = response["decision"]
            elif config["urban"]["policy"] in MODEL_POLICIES:
                from src.runtime.ship_onboard_model import verify_model_artifacts

                verify_model_artifacts(
                    root,
                    request["frames"],
                    history,
                    config["urban"]["policy"],
                    response["decision"],
                )
                expected = response["decision"]
            else:
                expected = choose_onboard_action(
                    history, config["urban"]["policy"], airspeed_mps=config["airspeed_mps"]
                )
            matches = [e for e in events if e.get("event") == "urban_decision"]
            if (
                len(matches) != 1
                or matches[0]["decision"] != expected
                or matches[0]["history"] != history
            ):
                reasons.append("onboard_decision_not_bound_to_images")
    if not decision_seen:
        reasons.append("onboard_decision_images_missing")
    if not errors or max(errors) > 2:
        reasons.append("onboard_projection_exceeds_two_metre_validation_envelope")
    return {
        "verified": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "image_observations_checked": len(errors),
        "maximum_obstacle_position_error_m": max(errors, default=None),
        "projection_limit_m": 2.0,
        "truth_used_for_policy": False,
        **({"anwm_verification": anwm_verification} if anwm_verification else {}),
    }
