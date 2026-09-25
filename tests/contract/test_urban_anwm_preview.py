"""Synthetic receipts exercise urban fail-closed bindings; no model/flight claims."""

import copy
import json
import math

import numpy as np
import pytest

from test_px4_anwm_input import (  # noqa: F401 - shared synthetic sensor fixture
    px4_request,
)
from scripts.aerial_anwm_runtime import (
    CONTEXT_SIZE,
    MODEL_REVISION,
    MODEL_SHA256,
    UPSTREAM_REVISION,
    VAE_REVISION,
    URBAN_SOURCE,
    digest_file,
    digest_json,
    urban_preview_candidates,
    validate_request,
)
from scripts.select_urban_wam_route import select_route, validate_live_observation
from scripts.urban_navigation_contract import scene_spec
from scripts.px4_aerial_flight_session import sign_command
from scripts.px4_urban_wam_trial import validate_trigger


@pytest.fixture
def urban_request(px4_request):  # noqa: F811 - pytest fixture injection
    request, arrays, base = px4_request
    scene = scene_spec("climb")
    request.update(
        source_kind=URBAN_SOURCE,
        num_timesteps=32,
        pose_conditioned_route_preview_only=True,
    )
    request["urban_preview_contract"] = {
        "schema_version": "missionos_urban_route_preview.v1",
        "scene_sha256": scene["scene_sha256"],
        "routes_enu_m": scene["routes"],
        "route_plans_sha256": digest_json(scene["routes"]),
        "prefix_path_length_m": 5.0,
        "time_alignment_verified": False,
    }
    provenance = request["px4_provenance"]
    provenance["scene_geometry_sha256"] = scene["scene_sha256"]
    provenance["goal_reference"]["scene_geometry_sha256"] = scene["scene_sha256"]
    request["candidates"] = urban_preview_candidates(
        np,
        scene["routes"],
        arrays["context_camera_poses"][-1],
        np.array([0, 0, -3.0]),
        0.0,
    )
    request["candidate_plans_sha256"] = digest_json(request["candidates"])
    return request, arrays, base, scene


def test_preview_follows_path_instead_of_straight_line_to_common_goal(urban_request):
    request, _, base, _ = urban_request
    _, manifest = validate_request(request, base)
    candidates = {c["candidate_id"]: c for c in manifest["candidates"]}
    assert candidates["climb"]["delta_local_m_rad"] == [0.0, 2.5, -2.5, 0.0]
    assert candidates["forward"]["delta_local_m_rad"] == [0.0, 5.0, 0.0, 0.0]
    assert manifest["pose_conditioned_route_preview_only"] is True
    assert manifest["model_time_alignment_verified"] is False


@pytest.mark.parametrize("change", ["route", "time", "source"])
def test_preview_cannot_relabel_path_or_timing_or_legacy_dispatch(
    urban_request, change
):
    request, _, base, _ = urban_request
    if change == "route":
        request["urban_preview_contract"]["routes_enu_m"]["climb"][0][0] += 1
    elif change == "time":
        request["model_time_alignment_verified"] = True
    else:
        request["source_kind"] = "px4_gazebo_frozen_capture"
    with pytest.raises(ValueError):
        validate_request(request, base)


@pytest.fixture
def selection_fixture(urban_request):
    request, _, base, scene = urban_request
    (base / "request.json").write_text(json.dumps(request))
    _, manifest = validate_request(request, base)
    outputs = []
    for i, candidate in enumerate(request["candidates"]):
        name = candidate["candidate_id"] + ".png"
        (base / name).write_bytes(b"synthetic-image-hash-fixture")
        outputs.append(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "goal_mse": [0.01, 0.2, 0.1][i],
                "projection_goal_mse": [0.01, 0.1, 0.2][i],
                "predicted_image": name,
                "projection_image": name,
                "predicted_image_sha256": digest_file(base / name),
                "projection_image_sha256": digest_file(base / name),
            }
        )
    result = {
        "schema_version": "aerial_anwm_result.v1",
        "input_manifest": manifest,
        "input_manifest_sha256": digest_json(manifest),
        "candidates": outputs,
        "model": {
            "model_id": "EmbodiedCity/ANWM",
            "checkpoint_sha256": MODEL_SHA256,
            "model_revision": MODEL_REVISION,
            "upstream_revision": UPSTREAM_REVISION,
            "vae_repository": "stabilityai/sd-vae-ft-ema",
            "vae_revision": VAE_REVISION,
            "context_size": CONTEXT_SIZE,
        },
        "runtime_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "model_execution_verified": True,
            "fixture_invocation": False,
            "model_sha256": MODEL_SHA256,
            "input_manifest_sha256": digest_json(manifest),
            "forecasts_sha256": digest_json(outputs),
            "actual_model_calls": 3,
            "sampling_steps": 250,
            "source_kind": URBAN_SOURCE,
            "execution_scope": "px4_gazebo_urban_pose_conditioned_route_preview",
            "model_time_alignment_verified": False,
        },
    }
    path = base / "result.json"
    path.write_text(json.dumps(result))
    now = request["px4_provenance"]["history"]["observed_at_unix_s"] + 60
    status = {
        "session_id": manifest["px4_provenance"]["session_id"],
        "scene_sha256": scene["scene_sha256"],
        "scene_static_verified": True,
        "phase": "holding",
        "armed": True,
        "px4_main_mode": 6,
        "hardware_target": False,
        "observed_at_unix_s": now,
        "telemetry_age_seconds": 0.1,
        "gazebo_pose_enu_m": [0, 0, 3],
        "local_ned_velocity_mps": [0, 0, 0],
        "yaw_ned_rad": 0,
    }
    return base, path, scene, status, now, result


def test_image_preference_cannot_admit_wall_crossing(selection_fixture):
    base, path, scene, status, now, _ = selection_fixture
    decision = select_route(base, path, scene, status, now)
    assert decision["unconstrained_model_choice"] == "forward"
    assert decision["route_id"] == "climb"
    assert decision["multiple_admissible_routes"] is False
    assert decision["learned_navigation_benefit_established"] is False


@pytest.mark.parametrize(
    "change",
    ["expired", "moved", "heading", "disarmed", "session", "nan", "stale_status"],
)
def test_live_consumer_rejects_stale_or_changed_hover(selection_fixture, change):
    _, _, scene, status, now, result = selection_fixture
    manifest = result["input_manifest"]
    if change == "expired":
        now += 181
    elif change == "moved":
        status["gazebo_pose_enu_m"][0] = 1
    elif change == "heading":
        status["yaw_ned_rad"] = 0.2
    elif change == "disarmed":
        status["armed"] = False
    elif change == "session":
        status["session_id"] = "other"
    elif change == "nan":
        status["local_ned_velocity_mps"][0] = math.nan
    else:
        status["observed_at_unix_s"] -= 2
    with pytest.raises(ValueError):
        validate_live_observation(manifest, scene, status, now)


def test_result_asset_substitution_is_rejected(selection_fixture):
    base, path, scene, status, now, result = selection_fixture
    (base / result["candidates"][0]["predicted_image"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        select_route(base, path, scene, status, now)


def test_signed_model_choice_still_cannot_change_to_unsafe_route():
    scene = scene_spec("climb")
    config = {
        "selection_mode": "anwm",
        "family": "climb",
        "session_id": "s",
        "scene_sha256": scene["scene_sha256"],
        "approved_instruction_ref": "retained-user-scope",
    }
    command = {k: v for k, v in config.items() if k not in ("selection_mode", "family")}
    command.update(
        route_id="climb",
        route_sha256=digest_json(scene["routes"]["climb"]),
        selection_sha256="a" * 64,
        issued_at_unix_s=100.0,
        expires_at_unix_s=104.0,
    )
    assert (
        validate_trigger(sign_command(command, b"key"), config, b"key", 101.0)
        == command
    )
    bad = copy.deepcopy(command)
    bad.update(route_id="forward", route_sha256=digest_json(scene["routes"]["forward"]))
    with pytest.raises(ValueError, match="admissible"):
        validate_trigger(sign_command(bad, b"key"), config, b"key", 101.0)
