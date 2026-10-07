"""Synthetic typed records, not integrated trajectories or flight evidence."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.runtime import starship_landing_decision_verifier as check
from src.runtime.starship_booster_recovery_verifier import _arrival, _rotate, _tower
from src.runtime.starship_sixdof_mission import _attitude


def fixture():
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    fuel, booster = 78000.0, profile["booster"]
    com = (booster["dry_mass_kg"] * booster["dry_com_z_m"] + fuel * booster["tank_center_z_m"]) / (
        booster["dry_mass_kg"] + fuel
    )

    def state(time):
        origin, axes = _tower(profile, time)
        east, _, up = axes
        q = [float(x) for x in _attitude(tuple(up), tuple(east))]
        height = catch["support_height_m"] + 3.55 - (60.0 - com) - 1.5 * (time - 100.0)
        r = [x + height * y for x, y in zip(origin, up)]
        v = [-7.292115e-5 * r[1] - 1.5 * up[0], 7.292115e-5 * r[0] - 1.5 * up[1], -1.5 * up[2]]
        return {
            "time_s": time,
            "r_eci_m": r,
            "v_eci_mps": v,
            "q_body_to_eci": q,
            "omega_body_rad_s": _rotate([q[0], -q[1], -q[2], -q[3]], [0.0, 0.0, 7.292115e-5]),
            "propellant_kg": fuel,
            "engine_states": [
                {"available": True, "throttle": 0.0, "gimbal_x_rad": 0.0, "gimbal_y_rad": 0.0}
                for _ in range(45)
            ],
            "flap_angles_rad": [0.0] * 6,
        }

    def command(powered=False):
        return {
            "engines": [
                {
                    "enabled": powered and i < 3,
                    "throttle": 0.4 if powered and i < 3 else 0.0,
                    "gimbal_x_rad": 0.0,
                    "gimbal_y_rad": 0.0,
                }
                for i in range(45)
            ],
            "flap_angles_rad": [0.0] * 6,
        }

    def point(time, phase="recovery_entry_coast", terminal=False):
        return {
            "time_s": time,
            "phase": phase,
            "state": state(time),
            "command": None if terminal else command(phase.startswith("recovery_landing")),
            "com_rate_body_mps": [0.0, 0.0, 0.0],
            "navigation": {},
        }

    plan = {
        "burn_axis_enu": [0.0, 0.0, 1.0],
        "target_velocity_enu_mps": [0.0, 0.0, 1.0],
        "bank_heading_enu": [1.0, 0.0, 0.0],
        "bank_sign": 1,
        "prediction_is_execution": False,
    }
    config = deepcopy(check._configuration())
    snapshot = {
        "schema": "missionos.starship_actual_recovery_context.v2",
        "state": state(100.0),
        "profile_sha256": check.digest(profile),
        "catch_profile_sha256": check.digest(catch),
        "guidance_configuration_sha256": check.digest(config),
        "physical_guidance_configuration": config,
        "context": {
            "phase": "recovery_entry_coast",
            "start_time_s": 95.0,
            "deadline_s": 1295.0,
            "plan": plan,
            "refreshed": True,
            "burn_start_s": 95.0,
            "settle_start_s": 96.0,
            "previous_entry_axis_enu": [0.0, 0.0, 1.0],
            "entry_pretrim_prepared_at_s": None,
            "next_preview_s": 100.0,
            "braking_preview": None,
            "prior_command_reference": {
                "quaternion": state(100.0)["q_body_to_eci"],
                "time_s": 100.0,
            },
            "conditioned_reference": {
                "quaternion": state(100.0)["q_body_to_eci"],
                "time_s": 100.0,
                "maximum_roll_rate_rad_s": 0.03 / 0.28,
                "bridging": True,
                "diagnostics": {},
                "frame_diagnostics": {},
            },
            "reference_tracker": None,
            "prepare_tail_start_s": 97.0,
        },
    }
    request = {
        "origin_context_sha256": check.digest(snapshot),
        "duration_s": 40.0,
        "maximum_integration_steps": 400,
        "wall_deadline_monotonic_s": 10000.0,
    }

    def run(points, policy, termination):
        return {
            "scenario": "booster_return",
            "body_id": "booster",
            "guidance_policy": policy,
            "initial_state": state(100.0),
            "booster_separation_state": state(100.0),
            "final_state": points[-1]["state"],
            "recovery_record": {
                "input_separation_state": state(100.0),
                "policy_id": policy,
                "guidance_configuration": deepcopy(config),
                "checkpoints": points,
            },
            "forecast_metadata": {
                "schema": "missionos.starship_actual_recovery_forecast.v1",
                "request": deepcopy(request),
                "origin_context_sha256": check.digest(snapshot),
                "failure": None,
                "controller_error": None,
                "final_context_error": None,
                "prediction_is_execution": False,
                "production_policy_admitted": False,
                "prediction_complete": termination == "catch_handoff",
                "landing_local_clock": {
                    "roundoff_tolerance_s": 1e-9,
                    "maximum_horizon_s": 40.0,
                    "maximum_integration_steps": 400,
                    "macrostep_preserved_within_clock_roundoff_only": True,
                },
            },
            "outcome": {
                "integration_steps": len(points) - 1,
                "start_time_s": 95.0,
                "end_time_s": points[-1]["time_s"],
                "duration_s": points[-1]["time_s"] - 95.0,
                "phase": points[-1]["phase"],
                "termination": termination,
                "physical_execution": False,
                "mission_completed": False,
            },
        }

    points = [point(100.0 + i * 0.1, terminal=i == 400) for i in range(401)]
    baseline = run(points, "constrained_return_development_v10", "time_limit")
    source = {
        "scenario": "booster_return",
        "body_id": "booster",
        "guidance_policy": "constrained_return_development_v11",
        "recovery_record": {
            "policy_id": "constrained_return_development_v11",
            "input_separation_state": state(100.0),
            "checkpoints": deepcopy(points),
            "plans": [{"plan": deepcopy(plan)} for _ in range(3)],
            "actual_planning_receipt": {
                "schema": "missionos.starship_actual_recovery_shooting.v3",
                "origin_context": deepcopy(snapshot),
                "baseline_equivalence": {"matched": True},
            },
        },
    }
    source_map = {"src/runtime/fixture_source.py": "a" * 64}
    actual = [check._physical_point(p) for p in points[:-1]]
    binding = {
        "schema": check.BASELINE_SCHEMA,
        "source_run_sha256": check.digest(source),
        "source_map_sha256": check.digest(source_map),
        "profile_sha256": check.digest(profile),
        "catch_profile_sha256": check.digest(catch),
        "physical_configuration_sha256": check.digest(config),
        "origin_context_sha256": check.digest(snapshot),
        "origin_state_sha256": check.digest(snapshot["state"]),
        "baseline_physical_checkpoints_sha256": check.digest(actual),
        "saved_physical_checkpoints_sha256": check.digest(actual),
        "baseline_checkpoint_count": 400,
        "saved_checkpoint_count": 400,
        "matched": True,
        "comparison_kind": check.COMPARISON_KIND,
        "source_binding_is_caller_assertion": True,
        "source_authentication_independently_verified": False,
        "dynamics_replayed": False,
        "prediction_is_execution": False,
    }
    prediction = run(
        [point(100.0, "recovery_landing_13"), point(100.1, "recovery_landing_13", True)],
        "constrained_return_development_v12",
        "catch_handoff",
    )
    prediction["recovery_record"]["guidance_configuration"]["landing_onset_decision"] = deepcopy(
        check.CONFIG
    )
    prediction["recovery_record"]["landing_onset_decision"] = {
        "schema": "missionos.starship_landing_onset_forecast_action.v1",
        "delay_s": 0.0,
        "origin_time_s": 100.0,
        "scheduled_onset_s": 100.0,
        "runtime_preview_calls": 0,
        "actual_state_assigned": False,
    }
    final = prediction["final_state"]
    actual = _arrival(final, profile, catch)
    assert actual["eligible"]
    # Closed tower observation uses the canonical stored format from the pure
    # arrival checker; the fixture computes no trajectory or dynamics step.
    obs = {
        "time_s": final["time_s"],
        "position_error_enu_m": actual["midpoint_enu_m"][:2]
        + [actual["midpoint_enu_m"][2] - catch["support_height_m"]],
        "pin_height_above_support_m": [p["height_above_support_m"] for p in actual["pins"]],
        "pins": [
            {
                "id": i,
                "position_body_m": catch["support_points_body_m"][i],
                "position_enu_m": p["position_enu_m"],
                "relative_velocity_enu_mps": p["velocity_enu_mps"],
                "footprint_active": False,
                "top_contact_eligible": False,
                "normal_force_n": 0.0,
                "force_enu_n": [0.0, 0.0, 0.0],
            }
            for i, p in enumerate(actual["pins"])
        ],
        "com_rate_body_mps": actual["com_rate_body_mps"],
        "tilt_deg": actual["tilt_deg"],
        "body_x_east_angle_deg": actual["clocking_error_deg"],
        "body_rate_rad_s": actual["body_rate_rad_s"],
        "propellant_kg": actual["propellant_kg"],
        "mass_kg": actual["mass_kg"],
        "com_body_m": actual["com_body_m"],
        "limits": actual["limits"],
        "eligible": True,
    }
    prediction["recovery_record"]["handoff"] = {
        "eligible": True,
        "time_s": final["time_s"],
        "state": deepcopy(final),
        "observation": obs,
    }
    decision = {
        "schema": check.DECISION_SCHEMA,
        "origin_time_s": 100.0,
        "origin_context": deepcopy(snapshot),
        "origin_state_sha256": check.digest(snapshot["state"]),
        "origin_operative_context_sha256": check.digest(check.operative_context(snapshot)),
        "delay_s": 0.0,
        "scheduled_onset_s": 100.0,
        "predicted_handoff_state_sha256": check.digest(final),
        "prediction_sha256": check.digest(prediction),
        "predicted_handoff_observation_sha256": check.digest(obs),
        "baseline_binding": deepcopy(binding),
        "runtime_preview_calls": 0,
        "prediction_is_execution": False,
        "arrival_admitted": False,
        "support_admitted": False,
        "physical_execution": False,
    }
    bundle = {
        "schema": check.BUNDLE_SCHEMA,
        "source_run_sha256": check.digest(source),
        "source_map_sha256": check.digest(source_map),
        "profile_sha256": check.digest(profile),
        "catch_profile_sha256": check.digest(catch),
        "separation_state_sha256": check.digest(
            source["recovery_record"]["input_separation_state"]
        ),
        "initial_plan": deepcopy(plan),
        "refreshed_plan": deepcopy(plan),
        "refresh_time_s": 100.0,
        "refresh_state_sha256": check.digest(snapshot["state"]),
        "refresh_operative_context_sha256": check.digest(check.operative_context(snapshot)),
        "source_binding_is_caller_assertion": True,
        "source_binding_independently_verified": False,
        "runtime_short_forecast_calls": 0,
        "runtime_actual_forecast_calls": 0,
        "prediction_is_execution": False,
        "physical_execution": False,
    }
    return (
        profile,
        catch,
        snapshot,
        source,
        source_map,
        baseline,
        binding,
        prediction,
        decision,
        bundle,
    )


def verdict(data):
    profile, catch, _, source, source_map, baseline, _, prediction, decision, _ = data
    return check.verify_landing_onset_decision(
        decision,
        profile,
        catch,
        source_run=source,
        baseline=baseline,
        prediction=prediction,
        source_map=source_map,
    )


def test_positive_synthetic_record_has_no_actual_arrival_or_source_attestation():
    data = fixture()
    before = deepcopy(data)
    result = verdict(data)
    assert result["passed"], result
    assert (
        result["baseline_common_checkpoints_match"] and result["predicted_full_handoff_consistent"]
    )
    assert all(
        result[k] is False
        for k in (
            "source_authentication_independently_verified",
            "dynamics_replayed",
            "arrival_admitted",
            "support_admitted",
            "mission_completed",
            "physical_execution",
            "model_value_established",
        )
    )
    assert data == before


@pytest.mark.parametrize(
    "change",
    [
        "matched_only",
        "source_hash",
        "source_map",
        "cfg_hash",
        "origin_hash",
        "count",
        "saved_digest",
        "changed_command",
        "extra_timestamp",
        "failure",
        "horizon",
        "steps",
        "authority",
    ],
)
def test_baseline_requires_raw_exact_common_command_points_not_matched_flag(change):
    data = list(fixture())
    profile, catch, snapshot, source, source_map, baseline, binding, _, _, _ = data
    if change == "matched_only":
        binding = {"matched": True}
    elif change == "source_hash":
        binding["source_run_sha256"] = "f" * 64
    elif change == "source_map":
        source_map["src/runtime/fixture_source.py"] = "f" * 64
    elif change == "cfg_hash":
        binding["physical_configuration_sha256"] = "f" * 64
    elif change == "origin_hash":
        binding["origin_state_sha256"] = "f" * 64
    elif change == "count":
        binding["saved_checkpoint_count"] -= 1
    elif change == "saved_digest":
        binding["saved_physical_checkpoints_sha256"] = "f" * 64
    elif change == "changed_command":
        source["recovery_record"]["checkpoints"][2]["command"]["flap_angles_rad"][3] = 0.01
        binding["source_run_sha256"] = check.digest(source)
    elif change == "extra_timestamp":
        source["recovery_record"]["checkpoints"].insert(
            3, deepcopy(source["recovery_record"]["checkpoints"][2])
        )
        binding["source_run_sha256"] = check.digest(source)
    elif change == "failure":
        baseline["forecast_metadata"]["failure"] = "ArithmeticError"
    elif change == "horizon":
        baseline["forecast_metadata"]["request"]["duration_s"] = 90.0
    elif change == "steps":
        baseline["forecast_metadata"]["request"]["maximum_integration_steps"] = 6500
    else:
        binding["source_authentication_independently_verified"] = True
    result = check.verify_landing_baseline(
        binding, snapshot, source, baseline, profile, catch, source_map=source_map
    )
    assert not result["passed"]


@pytest.mark.parametrize(
    "change",
    [
        "boolean_delay",
        "later_schedule",
        "prediction_hash",
        "handoff_state_hash",
        "handoff_observation_hash",
        "not_complete",
        "fuel",
        "tilt",
        "pin_speed",
        "no_handoff",
        "runtime_forecast",
        "admission",
    ],
)
def test_live_choice_requires_typed_schedule_and_complete_same_time_all_gates(change):
    data = list(fixture())
    prediction, decision = data[7:9]
    if change == "boolean_delay":
        decision["delay_s"] = False
    elif change == "later_schedule":
        decision["scheduled_onset_s"] += 2.0
    elif change == "prediction_hash":
        decision["prediction_sha256"] = "f" * 64
    elif change == "handoff_state_hash":
        decision["predicted_handoff_state_sha256"] = "f" * 64
    elif change == "handoff_observation_hash":
        decision["predicted_handoff_observation_sha256"] = "f" * 64
    elif change == "not_complete":
        prediction["forecast_metadata"]["prediction_complete"] = False
    elif change == "fuel":
        prediction["final_state"]["propellant_kg"] = 1.0
    elif change == "tilt":
        prediction["recovery_record"]["handoff"]["observation"]["tilt_deg"] = 5.0
    elif change == "pin_speed":
        prediction["recovery_record"]["handoff"]["observation"]["pins"][0][
            "relative_velocity_enu_mps"
        ][0] = 1.0
    elif change == "no_handoff":
        prediction["outcome"]["termination"] = "time_limit"
    elif change == "runtime_forecast":
        decision["runtime_preview_calls"] = 1
    else:
        decision["arrival_admitted"] = True
    if change not in ("prediction_hash", "handoff_state_hash", "handoff_observation_hash"):
        decision["prediction_sha256"] = check.digest(prediction)
    assert not verdict(data)["passed"]


def test_frozen_bundle_binds_selected_source_and_preserves_zero_runtime_forecasts():
    profile, catch, _, source, source_map, _, _, _, _, bundle = fixture()
    initial = source["recovery_record"]["input_separation_state"]
    result = check.verify_frozen_boostback_bundle(
        bundle, profile, catch, initial, source_run=source, source_map=source_map
    )
    assert result["passed"], result
    for key, value in (
        ("source_map_sha256", "f" * 64),
        ("runtime_short_forecast_calls", 1),
        ("source_binding_independently_verified", True),
        ("source_run_sha256", "f" * 64),
    ):
        changed = {**deepcopy(bundle), key: value}
        assert not check.verify_frozen_boostback_bundle(
            changed, profile, catch, initial, source_run=source, source_map=source_map
        )["passed"]


def test_actual_policy_header_requires_external_raw_landing_evidence():
    from src.runtime.starship_constrained_recovery_verifier import verify_constrained_recovery

    profile, catch, _, _, _, _, _, prediction, decision, bundle = fixture()
    record = prediction["recovery_record"]
    record.update(
        schema="missionos.starship_constrained_recovery.v1",
        production_policy_admitted=False,
        physical_execution=False,
        missionos_dispatch=False,
        frozen_boostback_bundle=bundle,
        landing_onset_decision=decision,
    )
    result = verify_constrained_recovery(prediction, prediction["initial_state"], profile, catch)
    assert not result["passed"] and result["issues"][0]["code"] == "landing_decision"
    assert not result["handoff_reached"] and not result["support"]


def test_local_clock_roundoff_marker_cannot_expand_horizon():
    profile, catch, snapshot, source, source_map, baseline, binding, _, _, _ = fixture()
    baseline["forecast_metadata"]["landing_local_clock"]["roundoff_tolerance_s"] = 0.01
    assert not check.verify_landing_baseline(
        binding, snapshot, source, baseline, profile, catch, source_map=source_map
    )["passed"]


def test_checker_configuration_and_imports_keep_old_physical_law_isolated():
    import ast
    from src.runtime.starship_constrained_recovery_verifier import _LANDING_CONFIGURATION

    assert _LANDING_CONFIGURATION == check.CONFIG
    imports = [
        node.module
        for node in ast.walk(ast.parse(Path(check.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        name
        not in (
            "starship_landing_decision",
            "starship_sixdof",
            "starship_physics",
            "starship_actual_recovery_shooting",
        )
        for name in imports
    )


def test_centroid_derivative_times_achieved_flow_keeps_finite_evaluation_order():
    from src.runtime.starship_booster_recovery_verifier import _flow_and_centroid

    profile, _, snapshot, *_ = fixture()
    state = deepcopy(snapshot["state"])
    state["propellant_kg"] = 78001.123
    state["engine_states"][0]["throttle"] = 0.4
    mass, _, reassociated, flow = _flow_and_centroid(state, profile)
    booster = profile["booster"]
    derivative = (booster["tank_center_z_m"] - booster["dry_com_z_m"]) * (
        booster["dry_mass_kg"] / mass**2
    )
    expected = [0.0, 0.0, derivative * (-flow)]
    assert check._centroid_rate_arithmetic(state, profile) == expected
    assert expected != reassociated  # Two mathematically equal expressions round differently.
    state["engine_states"][0]["available"] = False
    assert check._centroid_rate_arithmetic(state, profile) == [0.0, 0.0, 0.0]
