#!/usr/bin/env python3
"""Bound real ANWM image ranking to independently constrained urban route templates.

This is one initial route choice in opt-in SITL. It is not collision prediction,
receding-horizon replanning, or proof that WAM improves a geometric baseline.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aerial_anwm_runtime import (  # noqa: E402
    MODEL_SHA256,
    URBAN_SOURCE,
    digest_file,
    digest_json,
    validate_model_identity,
    validate_request,
)
from scripts.urban_navigation_contract import route_check, scene_spec  # noqa: E402


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_forecasts(input_dir, result_path):
    request = json.loads((input_dir / "request.json").read_text())
    _, manifest = validate_request(request, input_dir)
    require(manifest["source_kind"] == URBAN_SOURCE, "urban preview input required")
    result = json.loads(result_path.read_text())
    validate_model_identity(result.get("model", {}))
    require(
        result.get("schema_version") == "aerial_anwm_result.v1"
        and result.get("input_manifest") == manifest
        and result.get("input_manifest_sha256") == digest_json(manifest),
        "forecast input differs from revalidated local observation",
    )
    outputs = result.get("candidates", [])
    require(
        isinstance(outputs, list) and len(outputs) == len(manifest["candidates"]),
        "missing forecast candidate",
    )
    require(
        {o.get("candidate_id") for o in outputs}
        == {c["candidate_id"] for c in manifest["candidates"]},
        "forecast candidate set differs",
    )
    invocation = result.get("runtime_invocation_evidence", {})
    require(
        invocation.get("schema_version") == "runtime_invocation_evidence.v1"
        and invocation.get("model_execution_verified") is True
        and invocation.get("fixture_invocation") is False
        and invocation.get("model_sha256") == MODEL_SHA256
        and invocation.get("input_manifest_sha256") == digest_json(manifest)
        and invocation.get("forecasts_sha256") == digest_json(outputs)
        and invocation.get("actual_model_calls") == len(outputs)
        and invocation.get("sampling_steps") == 250
        and invocation.get("source_kind") == URBAN_SOURCE
        and invocation.get("execution_scope")
        == "px4_gazebo_urban_pose_conditioned_route_preview"
        and invocation.get("model_time_alignment_verified") is False,
        "real bound urban model invocation evidence required",
    )
    candidates = {c["candidate_id"]: c for c in manifest["candidates"]}
    for output in outputs:
        require(
            output.get("candidate_sha256")
            == candidates[output["candidate_id"]]["candidate_sha256"],
            "forecast action binding differs",
        )
        for field in ("goal_mse", "projection_goal_mse"):
            value = output.get(field)
            require(
                type(value) in (int, float)
                and math.isfinite(value)
                and 0 <= value <= 4,
                "non-finite or invalid image score",
            )
        for field in ("predicted_image", "projection_image"):
            name = output.get(field)
            require(
                isinstance(name, str)
                and Path(name).name == name
                and name.endswith(".png"),
                "invalid forecast asset name",
            )
            path = result_path.parent / name
            require(
                not path.is_symlink()
                and digest_file(path) == output.get(field + "_sha256"),
                "forecast image digest differs",
            )
    return manifest, outputs


def validate_live_observation(manifest, scene, status, now):
    provenance = manifest["px4_provenance"]
    observed = provenance["history"]["observed_at_unix_s"]
    require(
        type(now) in (float, int) and math.isfinite(now) and 0 <= now - observed <= 180,
        "urban model observation expired",
    )
    require(
        status.get("session_id") == provenance["session_id"]
        and status.get("scene_sha256") == scene["scene_sha256"]
        and status.get("scene_static_verified") is True
        and status.get("phase") == "holding"
        and status.get("armed") is True
        and status.get("px4_main_mode") == 6
        and status.get("hardware_target") is False,
        "live hover, scene or session differs",
    )
    require(
        0 <= now - status["observed_at_unix_s"] <= 1
        and status["telemetry_age_seconds"] < 1
        and math.dist(
            status["gazebo_pose_enu_m"], provenance["current_vehicle_gazebo_enu_m"]
        )
        <= 0.25
        and math.sqrt(sum(v * v for v in status["local_ned_velocity_mps"])) <= 0.15,
        "stale or moving hover cannot consume urban preview",
    )
    yaw_error = (status["yaw_ned_rad"] - provenance["yaw_ned_rad"] + math.pi) % (
        2 * math.pi
    ) - math.pi
    require(abs(yaw_error) <= 0.1, "heading moved since forecast observation")
    return now - observed


def select_route(input_dir, result_path, scene, status, now):
    require(scene == scene_spec(scene["family"]), "unsupported urban scene")
    manifest, outputs = validate_forecasts(input_dir, result_path)
    contract = manifest["urban_preview_contract"]
    require(
        contract["scene_sha256"] == scene["scene_sha256"]
        and contract["routes_enu_m"] == scene["routes"],
        "forecast routes differ from execution scene",
    )
    age = validate_live_observation(manifest, scene, status, now)
    checks = {
        name: route_check(scene, route, start=status["gazebo_pose_enu_m"])
        for name, route in scene["routes"].items()
    }
    admissible = [o for o in outputs if checks[o["candidate_id"]]["admissible"]]
    require(bool(admissible), "no independently admissible urban route")
    chosen = min(admissible, key=lambda o: (o["goal_mse"], o["candidate_id"]))[
        "candidate_id"
    ]
    projection = min(
        admissible, key=lambda o: (o["projection_goal_mse"], o["candidate_id"])
    )["candidate_id"]
    shortest = min(
        (name for name, check in checks.items() if check["admissible"]),
        key=lambda name: (checks[name]["route_length_m"], name),
    )
    return {
        "schema_version": "missionos_urban_wam_selection.v1",
        "selector": "anwm_goal_mse_among_geometry_admissible_routes",
        "session_id": status["session_id"],
        "scene_sha256": scene["scene_sha256"],
        "route_id": chosen,
        "route_sha256": digest_json(scene["routes"][chosen]),
        "model_forecast_used_for_dispatch": True,
        "jev_judgment_invoked": False,
        "input_manifest_sha256": digest_json(manifest),
        "model_result_sha256": digest_file(result_path),
        "model_sha256": MODEL_SHA256,
        "observation_age_seconds": age,
        "observation_unix_s": manifest["px4_provenance"]["history"][
            "observed_at_unix_s"
        ],
        "observation_enu_m": manifest["px4_provenance"]["current_vehicle_gazebo_enu_m"],
        "observation_yaw_ned_rad": manifest["px4_provenance"]["yaw_ned_rad"],
        "selected_at_unix_s": now,
        "route_checks": checks,
        "scores": {
            o["candidate_id"]: {
                "goal_mse": o["goal_mse"],
                "projection_goal_mse": o["projection_goal_mse"],
            }
            for o in outputs
        },
        "projection_choice": projection,
        "shortest_geometry_choice": shortest,
        "unconstrained_model_choice": min(
            outputs, key=lambda o: (o["goal_mse"], o["candidate_id"])
        )["candidate_id"],
        "multiple_admissible_routes": len(admissible) > 1,
        "score_is_collision_risk": False,
        "time_alignment_verified": False,
        "learned_navigation_benefit_established": False,
    }
