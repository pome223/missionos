"""Explicit opt-in visual extension to the unchanged M2 mission contract.

This first registered cohort is local fixture only. A transport supporting Jev
images does not itself authorize a hosted trial. Historical M2 authorities and
Director requests retain their original factories and exact field sets.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "missionos.starship_visual_authority.v1"
REQUEST_SCHEMA = "missionos.starship_visual_request.v1"
SCOPE = "local_simulation_visual_deployment_and_observed_return"
MODE_ENV = "MISSIONOS_STARSHIP_VISUAL_MODE"
CASES = ("normal", "delayed_confirmation", "primary_occluded", "retained_payload",
         "missing_frames", "unknown_inventory")
SCENARIOS = {"sixdof_visual_" + case: case for case in CASES}
ACTIONS = {"continue", "stop_deployment", "request_visual_observation",
           "request_numeric_confirmation"}
CHOICES = {"none", "next_frame_primary", "alternate_camera", "crop_existing",
           "fresh_separation_counter"}
REQUEST_KEYS = {
    "schema", "run_id", "request_id", "episode_id", "vehicle_id", "release_attempt_id",
    "event_id", "authority_sha256", "state_generation", "evidence_kind", "snapshot_time_s",
    "deadline_s", "manifest", "telemetry", "allowed_actions", "observation_choices",
    "prior_diagnosis",
}
EXTRA_SOURCES = (
    "src/runtime/starship_visual_contract.py",
    "src/runtime/starship_anomaly_events.py",
    "src/runtime/starship_visual_director.py",
    "src/runtime/starship_deployment_world.py",
    "src/runtime/starship_visual_observation.py",
    "src/runtime/starship_visual_runner.py",
    "src/runtime/starship_visual_verifier.py",
    "src/intelligence/starship_visual_agent.py",
    "src/intelligence/starship_visual_transport.py",
    "src/runtime/starship_runtime_budget.py",
    "scripts/run_starship_visual_observation.py",
    "scripts/smoke_starship_visual_gateway.py",
    "examples/spaceflight/starship-visual-observation-registration.json",
)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    return (type(value) is str and 1 <= len(value) <= 128
            and all(c.isascii() and (c.isalnum() or c in "_-") for c in value))


def hash_value(value):
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def visual_contract(case, *, mode="fixture"):
    from .starship_m2_contract import m2_contract

    if mode != "fixture" or case not in CASES:
        raise ValueError("visual_registered_fixture_only")
    inner_case = "release_fault" if case == "retained_payload" else "normal"
    return {
        "schema": SCHEMA, "scope": SCOPE, "mode": mode, "case": case,
        "m2_authority": m2_contract("fixture", inner_case),
        "visual": {
            "case": case, "confirmation_timeout_s": 2.0,
            "counter_source_max_age_s": 2.001, "frame_max_age_s": 3.0,
            "counter_delay_s": (None if case == "unknown_inventory" else
                                4.0 if case in ("delayed_confirmation", "missing_frames") else
                                3.0 if case == "primary_occluded" else 0.5),
            "counter_missing": case == "unknown_inventory",
            "maximum_episodes": 1, "maximum_judgments": 2, "maximum_observations": 1,
            "hold_s": 30.0, "decision_timeout_s": 8.0, "apply_margin_s": 2.0,
            "camera_fps": 2.0, "maximum_frames": 6, "maximum_frame_bytes": 262144,
            "maximum_image_bytes": 1572864, "maximum_wire_bytes": 3145728,
            "maximum_ring_bytes": 67108864, "maximum_store_bytes": 134217728,
            "ring_duration_s": 30.0,
            "camera_ids": ["deployment_port_primary", "deployment_port_secondary"],
        },
        "maximum_hosted_model_calls": 0, "maximum_local_http_attempts": 2,
        "maximum_wall_time_s": 2400.0, "maximum_output_bytes": 536870912,
        "minimum_free_bytes": 1073741824,
        "transport_mode": "loopback_fixture",
        "visual_model": "jev-1.14.0",
        "precautionary_hold_origin": "rules",
        "inventory_policy": "arrived_source_ordered_counter_then_fresh_closed_snapshot",
        "continuation_policy": "existing_m2_exact_state_executor",
        "human_approval_scope": "one_frozen_local_fixture_run",
        "physical_execution_authorized": False,
    }


def validate_authority(authority):
    if type(authority) is dict and authority.get("schema") == "missionos.starship_m3_visual_authority.v1":
        from .starship_unified_contract import validate_visual
        return validate_visual(authority)
    try:
        expected = visual_contract(authority["case"], mode=authority["mode"])
        if digest(authority) != digest(expected):
            raise ValueError("visual_authority_mismatch")
    except (KeyError, TypeError) as error:
        raise ValueError("visual_authority_invalid") from error
    return deepcopy(authority)


def validate_request(request):
    """Validate the public metadata envelope before any frame bytes are read."""
    if type(request) is not dict or set(request) != REQUEST_KEYS:
        raise ValueError("visual_request_fields")
    if request["schema"] != REQUEST_SCHEMA:
        raise ValueError("visual_request_version")
    for name in ("run_id", "request_id", "episode_id", "release_attempt_id", "event_id"):
        if not identity(request[name]):
            raise ValueError("visual_request_identity")
    if request["vehicle_id"] != "ship" or not hash_value(request["authority_sha256"]):
        raise ValueError("visual_request_binding")
    if type(request["state_generation"]) is not int or request["state_generation"] < 0:
        raise ValueError("visual_request_generation")
    start, end = request["snapshot_time_s"], request["deadline_s"]
    if not all(finite(v) and v >= 0 for v in (start, end)) or not start < end:
        raise ValueError("visual_request_deadline")
    if request["evidence_kind"] not in {"visual", "numeric_followup"}:
        raise ValueError("visual_request_evidence_kind")
    for field, allowed in (("allowed_actions", ACTIONS), ("observation_choices", CHOICES)):
        values = request[field]
        if (type(values) is not list or not values or any(type(v) is not str for v in values)
                or len(values) != len(set(values)) or not set(values) <= allowed):
            raise ValueError("visual_request_choices")
    if type(request["telemetry"]) is not dict or type(request["manifest"]) is not dict:
        raise ValueError("visual_request_evidence")
    from .starship_visual_observation import validate_evidence
    manifest = validate_evidence(request["manifest"], now=start, run_id=request["run_id"],
                                 vehicle_id=request["vehicle_id"], max_age_s=3.0)
    if manifest["snapshot_time_s"] != start:
        raise ValueError("visual_manifest_snapshot_binding")
    frames = manifest["frames"]
    if request["evidence_kind"] == "numeric_followup":
        if frames or request["prior_diagnosis"] is None:
            raise ValueError("visual_numeric_followup_history")
        if not set(request["allowed_actions"]) <= {"continue", "stop_deployment"}:
            raise ValueError("visual_numeric_followup_actions")
    elif request["prior_diagnosis"] is not None and type(request["prior_diagnosis"]) is not dict:
        raise ValueError("visual_prior_diagnosis")
    # Also reject NaN/Infinity nested in telemetry/metadata before hashing.
    digest(request)
    return deepcopy(request)


def sources():
    from .starship_m2_runner import sources as m2_sources
    return {**m2_sources(), **{name: sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in EXTRA_SOURCES}}


def plan_fields(mode, case, source_sha256):
    from .starship_m2_contract import PROFILE
    authority = visual_contract(case, mode=mode)
    return {
        "backend": "starship_visual_deployment_return",
        "mission_envelope": authority,
        "release_policy": "numeric_anomaly_visual_observation_confirmed_inventory_return",
        "model_authority": "fixture_only_bounded_visual_diagnosis",
        "jev_authority": "no_hosted_inference_in_registered_fixture",
        "simulation": {"case": case, "profile": PROFILE,
                       "profile_sha256": source_sha256[PROFILE], "dt_scale": 1.0,
                       "maximum_wall_time_s": authority["maximum_wall_time_s"]},
        "verification_scope": "visual_evidence_action_inventory_exact_state_return",
        "limitations": [
            "数値異常で放出を保留し、カメラ画像と追加観測を使う制御経路を検証します。",
            "fixtureは実HTTPで固定応答を返します。Jev推論・実機映像・実機飛行ではありません。",
            "画像所見で在庫を変更せず、独立counterが確定した場合だけ既存の帰還経路へ渡します。",
        ],
    }
