"""The HTTP smoke must identify the new runtime and preserve failed attempts."""

from copy import deepcopy

import pytest

from scripts import smoke_starship_chat_gateway as smoke


def evidence():
    plan = {
        "backend": "starship_sixdof",
        "simulation": {"scenario": "gimbal_step", "dt_scale": 1.0,
                       "duration_override_s": None, "profile_sha256": "a" * 64,
                       "return_policy": "fixed_v1"},
    }
    sample = {
        "time_s": 0.0, "r_eci_m": [7e6, 0, 0], "v_eci_mps": [0, 7000, 0],
        "q_body_to_eci": [1, 0, 0, 0], "omega_body_rad_s": [0, 0, 0],
    }
    study = {
        "schema": "missionos.starship_sixdof_study.v1",
        "provenance": {"physical_execution_invoked": False, "profile_sha256": "a" * 64,
                       "dt_scale": 1.0, "duration_override_s": None, "return_policy": "fixed_v1"},
        "runs": [{"scenario": "gimbal_step", "samples": [sample, {**sample, "time_s": 30.0}],
                  "outcome": {"six_dof_integrated": True, "attitude_prescribed": False,
                              "duration_s": 30.0}}],
    }
    verification = {"passed": True, "mission_completed": False, "physical_execution": False,
                    "observed_outcomes": [{"termination": "time_limit"}]}
    return plan, study, verification


def test_sixdof_http_evidence_keeps_initialized_response_distinct_from_mission_success():
    result = smoke.sixdof_evidence("sixdof_gimbal_step", *evidence())
    assert result["six_dof_integrated"] is True
    assert result["simulation_duration_s"] == 30
    assert result["observed_outcomes"] == [{"termination": "time_limit"}]
    assert result["starship_vehicle_validated"] is False


@pytest.mark.parametrize("kind", ["old_backend", "wrong_case", "shortened", "wrong_profile",
                                 "prescribed", "nonfinite", "bad_quaternion", "no_time", "false_success", "wrong_policy"])
def test_sixdof_http_evidence_rejects_runtime_substitution_and_false_claims(kind):
    plan, study, verification = deepcopy(evidence())
    run = study["runs"][0]
    if kind == "old_backend":
        plan["backend"] = "starship_3d_reference"
    elif kind == "wrong_case":
        run["scenario"] = "launch"
    elif kind == "shortened":
        study["provenance"]["duration_override_s"] = 1.0
    elif kind == "wrong_profile":
        study["provenance"]["profile_sha256"] = "b" * 64
    elif kind == "prescribed":
        run["outcome"]["attitude_prescribed"] = True
    elif kind == "nonfinite":
        run["samples"][-1]["omega_body_rad_s"] = [0, 0, float("nan")]
    elif kind == "bad_quaternion":
        run["samples"][-1]["q_body_to_eci"] = [1, 1, 1, 1]
    elif kind == "no_time":
        run["samples"][-1]["time_s"] = 0
    elif kind == "wrong_policy":
        study["provenance"]["return_policy"] = "mass_state_terminal_v1"
    else:
        verification["mission_completed"] = True
    with pytest.raises(AssertionError):
        smoke.sixdof_evidence("sixdof_gimbal_step", plan, study, verification)


def test_smoke_preserves_previous_failure_before_any_network_request(tmp_path, monkeypatch):
    failure = tmp_path / "chat-transcript.json"
    failure.write_text('[{"failed": true}]')
    monkeypatch.setattr(smoke, "urlopen", lambda *_a, **_k: pytest.fail("network must not run"))
    with pytest.raises(ValueError, match="starship_smoke_output_must_be_empty"):
        smoke.run(18794, "sixdof_launch", tmp_path)
    assert failure.read_text() == '[{"failed": true}]'


def test_retained_return_http_evidence_separates_policy_verification_from_mission_result():
    plan, study, verification = evidence()
    plan["simulation"].update(scenario="deployment_no_effect", return_policy="mass_state_terminal_v1")
    plan["return_policy"] = {"policy_id": "mass_state_terminal_v1"}
    plan["flight_supervision"] = {"allowed_actions": ["hold", "skip_remaining_deployment"]}
    study["provenance"]["return_policy"] = "mass_state_terminal_v1"
    run = study["runs"][0]
    run.update(scenario="deployment_no_effect", retained_return={"policy_id": "mass_state_terminal_v1"},
               supervision={"request": {"allowed_actions": plan["flight_supervision"]["allowed_actions"]}})
    verification.update(retained_return={"passed": True}, flight_supervision={"passed": True},
                        observed_outcomes=[{"termination": "surface_impact"}])
    result = smoke.sixdof_evidence("sixdof_retained_return_supervised", plan, study, verification)
    assert result["retained_return"]["passed"] is True
    assert result["observed_outcomes"] == [{"termination": "surface_impact"}]
    run["retained_return"]["policy_id"] = "fixed_v1"
    with pytest.raises(AssertionError):
        smoke.sixdof_evidence("sixdof_retained_return_supervised", plan, study, verification)
