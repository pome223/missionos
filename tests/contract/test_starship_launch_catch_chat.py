"""Continuous return is a distinct approval scope; honest failure remains valid."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import smoke_starship_chat_gateway as smoke
from src.gateway.starship_chat import _response
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime.starship_sixdof_catalog import BOOSTER_RECOVERY_POLICY, LAUNCH_CATCH_SCENARIO


@pytest.mark.parametrize("utterance,scenario", [
    ("Starshipを打ち上げからキャッチまで", LAUNCH_CATCH_SCENARIO),
    ("打ち上げから帰還してキャッチ", LAUNCH_CATCH_SCENARIO),
    ("連続キャッチを試したい", LAUNCH_CATCH_SCENARIO),
    ("Starship launch-to-catch", LAUNCH_CATCH_SCENARIO),
    ("Starship continuous catch", LAUNCH_CATCH_SCENARIO),
    ("Starship booster catch", "sixdof_booster_catch"),
    ("Starshipキャッチ", "sixdof_booster_catch"),
    ("Starship タワー故障のキャッチ", "sixdof_booster_catch_tower_unavailable"),
    ("Starship sixdof_booster_catch。打ち上げからキャッチとの比較用", "sixdof_booster_catch"),
])
def test_full_launch_phrase_is_distinct_from_initialized_catch_and_explicit_catalog(utterance, scenario):
    result = plan_starship_request(utterance, "fixture")
    assert result["proposal"]["scenario"] == scenario
    assert result["invocation"]["approval_granted"] is False
    assert result["invocation"]["model_inference_invoked"] is False


def plan():
    return {"id": "plan", "sha256": "a"*64, "scenario": LAUNCH_CATCH_SCENARIO,
            "backend": "starship_sixdof", "rationale": "bounded return", "uncertainties": ["Unidentified hardware"],
            "booster_recovery": {"policy_id": BOOSTER_RECOVERY_POLICY,
                                 "capture_planning_contract": "missionos.starship_capture_planning.v1"},
            "simulation": {"scenario": "launch", "profile_id": "development", "profile_sha256": "b"*64,
                           "catch_profile_sha256": "c"*64, "booster_policy": BOOSTER_RECOVERY_POLICY,
                           "return_policy": "fixed_v1", "dt_scale": 1., "duration_override_s": None,
                           "maximum_wall_time_s": 300}, "verification_scope": "saved output consistency", "limitations": ["Not certified"]}


def evidence():
    p = plan()
    sample = {"time_s": 0., "r_eci_m": [7e6, 0., 0.], "v_eci_mps": [0., 7000., 0.],
              "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.]}
    outcome = {"scenario": "launch", "termination": "surface_impact", "orbit_gate_reached": True,
               "payload_released_count": 26, "booster": {"return_site_distance_m": 9000., "final_ground_speed_mps": 289.}}
    study = {"schema": "missionos.starship_sixdof_study.v1",
             "provenance": {"physical_execution_invoked": False, "profile_sha256": "b"*64,
                            "catch_profile_sha256": "c"*64, "return_policy": "fixed_v1",
                            "booster_policy": BOOSTER_RECOVERY_POLICY, "dt_scale": 1., "duration_override_s": None},
             "runs": [{"scenario": "launch", "samples": [sample, {**sample, "time_s": 600.}],
                       "booster_run": {"outcome": {"termination": "surface_contact"},
                                       "recovery_record": {"full_coast_prediction_count": 3,
                                           "capture_planning": {"admissible_prediction_count": 0}}},
                       "outcome": {"six_dof_integrated": True, "attitude_prescribed": False, "duration_s": 600.}}]}
    verdict = {"passed": True, "mission_completed": False, "physical_execution": False,
               "booster_recovery": {"passed": True, "handoff_reached": False, "catch_supported_after_handoff": False,
                   "capture_planning": {"assessed": True, "forecast_count": 3, "admissible_prediction_count": 0,
                                        "best_effort_cutoff_used": True, "dispatch_authority_created": False,
                                        "forecast_dynamics_reexecuted": False}},
               "launch_connected_catch_supported": False, "observed_outcomes": [outcome]}
    return p, study, verdict


def test_preflight_explains_continuous_scope_without_claiming_capture():
    result = _response("Starship 打ち上げからキャッチ", "plan", {"status": "awaiting_approval", "plan": plan(), "execution": {}}, "operator")
    assert "実際の段分離状態" in result["message"]
    assert "引継ぎを承認" in result["message"]
    assert "失敗を保存" in result["message"]
    assert "成功や実機の安全性を保証する承認ではありません" in result["message"]
    assert "初期化したキャッチ実験へのGO" not in result["message"]


def test_verified_failed_return_does_not_become_successful_catch_in_chat_or_smoke():
    p, study, verdict = evidence()
    summary = smoke.sixdof_evidence(LAUNCH_CATCH_SCENARIO, p, study, verdict)
    assert summary["booster_recovery"]["passed"] is True
    assert summary["launch_connected_catch_supported"] is False
    result = _response("/status", "status", {"status": "verified", "plan": p,
        "execution": {"verification": verdict, "artifacts": []}}, "operator")
    assert "引継ぎ 未達" in result["message"]
    assert "支持・静定 未達" in result["message"]
    assert "最終目標距離 9,000.000 m" in result["message"]
    assert "ミッション成功の認定ではありません" in result["message"]
    assert "0 / 3件" in result["message"]
    assert "成立計画を得られず" in result["message"]


def test_verified_launch_before_separation_renders_unreached_without_booster():
    from src.runtime.starship_sixdof_mission import simulate
    from src.runtime.starship_sixdof_verifier import verify_study
    root = Path(__file__).resolve().parents[2]
    profile = json.loads((root / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch_config = json.loads((root / "examples/spaceflight/starship-catch-profile.json").read_text())
    run = simulate(profile, duration_s=.2, booster_policy=BOOSTER_RECOVERY_POLICY, catch_config=catch_config)
    study = {"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
             "catch_profile": catch_config, "runs": [run],
             "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False,
                            "booster_policy": BOOSTER_RECOVERY_POLICY}}
    verdict = verify_study(json.loads(json.dumps(study)), expected_scenario="launch")
    assert verdict["passed"] is True, verdict
    assert verdict["observed_outcomes"][0]["booster"] is None
    result = _response("/status", "status", {"status": "verified", "plan": plan(),
        "execution": {"verification": verdict, "artifacts": []}}, "operator")
    assert "引継ぎ 未達" in result["message"]
    assert "支持・静定 未達" in result["message"]
    assert "最終目標距離" not in result["message"]


@pytest.mark.parametrize("mutation", ["policy", "catch_profile", "missing_recovery_verification", "false_capture", "missing_booster", "untyped_handoff",
                                     "unassessed_planning", "planning_authority", "planning_count"])
def test_continuous_smoke_rejects_scope_substitution_and_unsupported_capture(mutation):
    p, study, verdict = deepcopy(evidence())
    if mutation == "policy":
        study["provenance"]["booster_policy"] = "fixed_v1"
    elif mutation == "catch_profile":
        study["provenance"]["catch_profile_sha256"] = "d"*64
    elif mutation == "missing_recovery_verification":
        verdict["booster_recovery"]["passed"] = False
    elif mutation == "false_capture":
        verdict["launch_connected_catch_supported"] = True
    elif mutation == "missing_booster":
        study["runs"][0]["booster_run"] = None
    elif mutation == "untyped_handoff":
        verdict["booster_recovery"]["handoff_reached"] = "yes"
    elif mutation == "unassessed_planning":
        verdict["booster_recovery"]["capture_planning"]["assessed"] = False
    elif mutation == "planning_authority":
        verdict["booster_recovery"]["capture_planning"]["dispatch_authority_created"] = True
    elif mutation == "planning_count":
        verdict["booster_recovery"]["capture_planning"]["admissible_prediction_count"] = 1
    with pytest.raises(AssertionError):
        smoke.sixdof_evidence(LAUNCH_CATCH_SCENARIO, p, study, verdict)
