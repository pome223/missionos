"""Concretize retained SITL authorization after screening actual static SDF boxes.

No approval is generated, model called, or aircraft commanded. A 0.6 m margin
around the airframe reference point must clear every declared collision box.
The executor separately checks the observed scene remains unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

import numpy as np

from missionos_core.prediction import prediction_digest
from scripts.evaluate_px4_aerial_wam_jev import IDS, MODEL_SHA256, _policy_targets, validate_result
from scripts.screen_px4_aerial_candidates import segment_intersects_box

AIRFRAME_MARGIN_M = 0.6


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(directory, name):
    _require(Path(name).name == name and name not in ("", ".", ".."), "unsafe_source_filename")
    path = directory / name
    _require(not path.is_symlink(), "source_symlink_not_allowed")
    return path.read_bytes()


def _vector(value, length=3):
    _require(isinstance(value, list) and len(value) == length
             and all(type(v) in (int, float) and math.isfinite(v) for v in value), "invalid_geometry_vector")
    return np.asarray(value, dtype=float)


def _pose(element):
    if element is None:
        return np.zeros(6)
    _require(not element.attrib, "relative_sdf_pose_not_supported")
    return _vector([float(v) for v in (element.text or "").split()], 6)


def verify_scene(session_dir, config):
    """Reproduce the scene digest from collision geometry and exact SDF bytes."""
    session_dir = Path(session_dir)
    scene_bytes = _read(session_dir, "scene.json")
    scene = json.loads(scene_bytes)
    _require(scene.get("schema_version") == "missionos_px4_calibration_scene.v1", "unsupported_scene_schema")
    boxes = scene["boxes"]
    _require(isinstance(boxes, list) and bool(boxes), "declared_collision_boxes_required")
    records, names = [], set()
    entities = config["scene_entities"]
    _require(isinstance(entities, list), "observed_scene_binding_required")
    for box in boxes:
        name = box["name"]
        _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name)
                 and name not in names, "invalid_or_duplicate_scene_entity")
        names.add(name)
        center, size = _vector(box["center_xyz_m"]), _vector(box["size_xyz_m"])
        _require(np.all(size > 0), "nonpositive_collision_box")
        sdf = _read(session_dir, name + ".sdf")
        tree = ET.fromstring(sdf)
        models = tree.findall("model")
        _require(len(models) == 1, "one_static_model_per_sdf_required")
        model = models[0]
        _require(model.get("name") == name and model.findtext("static") == "true"
                 and not model.findall(".//include") and not model.findall(".//plugin"),
                 "unsupported_dynamic_or_composed_scene")
        model_pose = _pose(model.find("pose"))
        links, collisions = model.findall("link"), model.findall(".//collision")
        _require(len(links) == 1 and len(collisions) == 1, "one_collision_box_per_static_model_required")
        _require(np.allclose(_pose(links[0].find("pose")), 0, atol=1e-12, rtol=0)
                 and np.allclose(_pose(collisions[0].find("pose")), 0, atol=1e-12, rtol=0),
                 "offset_collision_geometry_not_supported")
        geometries = list(collisions[0].find("geometry"))
        _require(len(geometries) == 1 and geometries[0].tag == "box", "only_axis_aligned_boxes_supported")
        dimensions = _vector([float(v) for v in geometries[0].findtext("size", "").split()])
        _require(np.allclose(model_pose[:3], center, atol=1e-9, rtol=0)
                 and np.allclose(model_pose[3:], 0, atol=1e-12, rtol=0)
                 and np.array_equal(dimensions, size), "scene_description_and_sdf_disagree")
        observed = [e for e in entities if e.get("name") == name]
        _require(len(observed) == 1, "scene_entity_not_bound_in_controller")
        entity = observed[0]
        _require(np.allclose(_vector([entity["position"].get(k, 0) for k in "xyz"]), center, atol=1e-9, rtol=0)
                 and np.allclose(_vector([entity["orientation"].get(k, 0) for k in "xyzw"], 4), [0, 0, 0, 1], atol=1e-12, rtol=0),
                 "controller_scene_entity_pose_mismatch")
        records.append({"name": name, "center_xyz_m": center.tolist(), "size_xyz_m": size.tolist(),
                        "sdf_sha256": _sha(sdf)})
    # A reference camera has no collision geometry. Unknown entities require
    # their own geometry audit instead of being silently assumed clear.
    _require({e["name"] for e in entities} - names <= {"aerial_goal_reference"}, "unaudited_scene_entity")
    if any(e["name"] == "aerial_goal_reference" for e in entities):
        goal_sdf = ET.fromstring(_read(session_dir, "goal-camera.sdf"))
        model = goal_sdf.find("model")
        _require(model is not None and model.get("name") == "aerial_goal_reference"
                 and model.findtext("static") == "true" and not model.findall(".//collision")
                 and not model.findall(".//include") and not model.findall(".//plugin"),
                 "goal_camera_collision_geometry_not_audited")
    digest = prediction_digest(records)
    _require(digest == scene.get("scene_geometry_sha256") == config.get("scene_sha256"), "scene_geometry_hash_mismatch")
    return records, digest, _sha(scene_bytes)


def build_policy(result, session_dir, *, now=None, expected_model_sha256=MODEL_SHA256):
    now = time.time() if now is None else now
    _require(type(now) in (int, float) and math.isfinite(now), "invalid_policy_time")
    session_dir = Path(session_dir)
    manifest, _, _ = validate_result(result, expected_model_sha256=expected_model_sha256)
    config_bytes = _read(session_dir, "config.json")
    config = json.loads(config_bytes)
    _require(config.get("schema_version") == "missionos_px4_aerial_flight_policy.v1"
             and config.get("execution_scope") == "px4_sitl" and config.get("hardware_target") is False
             and config.get("candidate_ids") == list(IDS), "existing_bounded_sitl_session_required")
    provenance = manifest["px4_provenance"]
    _require(config.get("session_id") == provenance["session_id"], "model_session_mismatch")
    records, scene_hash, scene_file_hash = verify_scene(session_dir, config)
    _require(provenance["scene_geometry_sha256"] == scene_hash, "model_scene_mismatch")
    authorization_bytes = _read(session_dir, "user-authorization.json")
    authorization = json.loads(authorization_bytes)
    _require(isinstance(authorization.get("instruction_ref"), str)
             and authorization["instruction_ref"] == config["approved_instruction_ref"]
             and isinstance(authorization.get("text"), str) and bool(authorization["text"].strip())
             and authorization.get("hardware_allowed") is False,
             "retained_user_instruction_record_required")
    authorized_at = authorization["authorized_at_unix_s"]
    _require(type(authorized_at) in (int, float) and math.isfinite(authorized_at)
             and authorized_at <= now, "authorization_time_invalid")
    start_ned = _vector(provenance["current_vehicle_local_ned_m"])
    yaw = provenance["yaw_ned_rad"]
    start_enu = start_ned[[1, 0, 2]] * [1, 1, -1]
    candidates, screens = [], []
    for candidate, right in zip(manifest["candidates"], (-5.0, 5.0)):
        target_ned = start_ned + [-math.sin(yaw) * right, math.cos(yaw) * right, 0]
        target_enu = target_ned[[1, 0, 2]] * [1, 1, -1]
        intersections = []
        for box in records:
            center, half = _vector(box["center_xyz_m"]), _vector(box["size_xyz_m"]) / 2
            if segment_intersects_box(start_enu, target_enu, center - half - AIRFRAME_MARGIN_M,
                                      center + half + AIRFRAME_MARGIN_M):
                intersections.append(box["name"])
        _require(min(start_enu[2], target_enu[2]) > AIRFRAME_MARGIN_M,
                 "airframe_margin_intersects_ground_plane")
        _require(not intersections, "candidate_airframe_path_intersects_sdf:" + candidate["candidate_id"])
        screen = {"candidate_id": candidate["candidate_id"], "candidate_sha256": candidate["candidate_sha256"],
                  "airframe_start_enu_m": start_enu.tolist(), "airframe_end_enu_m": target_enu.tolist(),
                  "margin_m_per_axis": AIRFRAME_MARGIN_M, "intersected_collision_boxes": intersections,
                  "screen_method": "closed_segment_vs_margin_expanded_static_sdf_boxes.v1"}
        screens.append(screen)
        candidates.append({"candidate_id": candidate["candidate_id"], "candidate_sha256": candidate["candidate_sha256"],
                           "target_local_ned_m": target_ned.tolist(), "independent_sdf_path_clear": True,
                           "geometry_screen_sha256": prediction_digest(screen)})
    policy = {
        "schema_version": "missionos_px4_aerial_execution_policy.v1", "execution_scope": "px4_sitl",
        "session_id": config["session_id"], "scene_sha256": scene_hash, "static_scene": True,
        "authorization": {"kind": "explicit_user_instruction", "instruction_ref": authorization["instruction_ref"],
                          "instruction_sha256": _sha(authorization["text"].encode()),
                          "authorized_at_unix_s": authorized_at, "expires_at_unix_s": now + 7200,
                          "hardware_allowed": False, "retained_record_sha256": _sha(authorization_bytes)},
        "allowed_candidates": candidates,
        "geofence": {"north_min_m": -6.0, "north_max_m": 6.0, "east_min_m": -6.0, "east_max_m": 6.0,
                     "altitude_min_m": 2.8, "altitude_max_m": 3.2},
        "geometry_verification": {"schema_version": "missionos_px4_static_sdf_corridor_screen.v1",
                                  "scene_geometry_sha256": scene_hash, "scene_file_sha256": scene_file_hash,
                                  "controller_config_sha256": _sha(config_bytes), "boxes": records,
                                  "candidate_screens": screens, "airframe_margin_m": AIRFRAME_MARGIN_M,
                                  "scope": "declared_static_collision_boxes_and_ground_plane",
                                  "dynamic_scene_safety_established": False, "flight_outcome_observed": False},
        "approval_generated": False, "dispatch_invoked": False,
    }
    _policy_targets(manifest, policy, now)
    return policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _require(not args.output.exists(), "policy_output_must_be_new")
    policy = build_policy(json.loads(args.result.read_text()), args.session_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        handle.write(json.dumps(policy, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"policy_sha256": prediction_digest(policy), "candidate_count": len(policy["allowed_candidates"]),
                      "airframe_margin_m": AIRFRAME_MARGIN_M, "approval_generated": False, "dispatch_invoked": False}))


if __name__ == "__main__":
    main()
