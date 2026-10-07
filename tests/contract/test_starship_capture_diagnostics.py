"""Analytic material-point and force diagnostics, without trajectory integration."""
from copy import deepcopy
import inspect
import json
import math
from pathlib import Path

import pytest

from scripts import diagnose_starship_capture as cli
from src.runtime import starship_capture_diagnostics as diagnostics
from src.runtime import starship_capture_diagnostic_report as rendering

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    config = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    profile["launch"].update(latitude_deg=0., longitude_deg=0.)
    fuel, dry = 30000., profile["booster"]["dry_mass_kg"]
    com = (dry*profile["booster"]["dry_com_z_m"]+fuel*profile["booster"]["tank_center_z_m"])/(dry+fuel)
    radius = 6378137.+config["support_height_m"]+3.4-(config["support_points_body_m"][0][2]-com)
    state = {"time_s": 0., "r_eci_m": [radius, 0., 0.], "v_eci_mps": [-1., 7.292115e-5*radius, 0.],
             "q_body_to_eci": [.5, .5, .5, .5], "omega_body_rad_s": [0., 7.292115e-5, 0.],
             "propellant_kg": fuel, "flap_angles_rad": [0.]*6,
             "engine_states": [{"available": True, "throttle": .55 if i < 2 else 0.,
                                "gimbal_x_rad": 0., "gimbal_y_rad": 0.} for i in range(45)]}
    return profile, config, state


def test_analytic_pin_velocity_and_simultaneous_margins(setup):
    profile, config, state = setup
    row = diagnostics.state_diagnostic(state, profile, config)
    dry = profile["booster"]["dry_mass_kg"]
    flow = 1.1*profile["booster"]["engine_thrust_n"]/(profile["booster"]["engine_isp_s"]*9.80665)
    com_rate = -flow*dry*(profile["booster"]["tank_center_z_m"]-profile["booster"]["dry_com_z_m"])/(dry+30000.)**2
    assert not row["failed_gates"] and row["arrival"]["eligible"]
    assert row["margins"]["horizontal_position"] == pytest.approx(.2)
    for pin in row["arrival"]["pins"]:
        assert pin["height_above_support_m"] == pytest.approx(3.4)
        assert pin["velocity_enu_mps"] == pytest.approx([0., 0., -1.-com_rate], abs=1e-9)


@pytest.mark.parametrize("gate", list(diagnostics.GATE_UNITS))
def test_no_gate_is_replaced_by_another_gates_margin(setup, gate):
    profile, config, state = setup
    arrival = diagnostics.checks._arrival(state, profile, config)
    if gate == "horizontal_position":
        arrival["midpoint_enu_m"][0] = .21
    elif gate == "pin_height":
        arrival["pins"][1]["height_above_support_m"] = 3.81
    elif gate == "pin_vertical_speed":
        arrival["pins"][1]["velocity_enu_mps"][2] = -.49
    elif gate == "pin_horizontal_speed":
        arrival["pins"][1]["velocity_enu_mps"][0] = .41
    elif gate == "tilt":
        arrival["tilt_deg"] = arrival["limits"]["attitude_angle_deg"]+.01
    elif gate == "clocking":
        arrival["clocking_error_deg"] = arrival["limits"]["attitude_angle_deg"]+.01
    elif gate == "body_rate":
        arrival["body_rate_rad_s"] = .011
    else:
        arrival["propellant_kg"] = arrival["limits"]["propellant_reserve_kg"]-1.
    margins = diagnostics.gate_margins(arrival)
    assert margins[gate] < 0 and all(value >= 0 for key, value in margins.items() if key != gate)


def test_actual_gimbal_and_inverted_main_thrust(setup):
    profile, _, state = setup
    state["engine_states"][1]["available"] = False
    state["engine_states"][0]["gimbal_y_rad"] = .1
    thrust = .55*profile["booster"]["engine_thrust_n"]
    assert diagnostics.main_thrust(state, profile)["force_enu_n"][2] == pytest.approx(thrust*math.cos(.1))
    state["q_body_to_eci"] = [-.5, -.5, .5, .5]
    assert diagnostics.main_thrust(state, profile)["force_enu_n"][2] == pytest.approx(-thrust*math.cos(.1))
    # Spool state alone does not produce a force after the reservoir empties.
    state["propellant_kg"] = 0.
    assert diagnostics.main_thrust(state, profile)["force_enu_n"] == [0., 0., 0.]


def local_record(setup, feasible=True):
    profile, config, state = setup
    end = deepcopy(state)
    end["time_s"] = .5
    preview = {"burn_fuel_feasible": feasible, "estimated_burn_propellant_kg": 23000., "estimated_burn_time_s": .4,
               "estimated_horizontal_displacement_m": [1., 0.], "estimated_stopping_height_m": .1}
    run = {"events": [{"event": "landing_stage_requested", "requested_engine_count": 13, "time_s": 0.,
                       "state": state, "prediction": preview}], "final_state": end,
           "outcome": {"termination": "surface_contact", "final_ground_speed_mps": 1.},
           "recovery_record": {"checkpoints": [{"time_s": .5, "state": end, "phase": "recovery_landing_13",
                                                 "command": None, "navigation": {}}]}}
    return run, profile, config


def test_preview_reserve_uses_remaining_mass_and_is_not_a_stop_observation(setup):
    run, profile, config = local_record(setup)
    result = diagnostics.analyze_run(run, profile, config)
    expected = (profile["booster"]["dry_mass_kg"]+7000.)*9.81/(profile["booster"]["engine_isp_s"]*9.80665)*result["landing_request"]["arrival"]["limits"]["reserve_horizon_s"]
    assert result["ideal_preview_capture_reserve_kg"] == pytest.approx(expected)
    assert result["ideal_preview_fuel_surplus_kg"] == pytest.approx(7000.-expected)
    assert result["elapsed_to_terminal_s"] == .5 and not result["dynamics_reexecuted"]


def test_incomplete_preview_consumption_is_not_required_stopping_fuel(setup):
    run, profile, config = local_record(setup, feasible=False)
    result = diagnostics.analyze_run(run, profile, config)
    assert result["ideal_preview_fuel_surplus_kg"] is None
    assert result["ideal_preview_terminal_fuel_kg"] is None


def test_preview_can_arrest_speed_but_lack_capture_reserve(setup):
    run, profile, config = local_record(setup)
    run["events"][0]["prediction"]["estimated_burn_propellant_kg"] = 25000.
    result = diagnostics.analyze_run(run, profile, config)
    assert result["preview"]["burn_fuel_feasible"] and result["ideal_preview_fuel_surplus_kg"] < 0


def test_approximate_stop_point_projects_cg_frame_and_vertical_motion(setup):
    profile, _, state = setup
    radius, angle = math.hypot(*state["r_eci_m"][:2]), .1
    state["r_eci_m"] = [radius*math.cos(angle), radius*math.sin(angle), 0.]
    preview = {"burn_fuel_feasible": True, "estimated_horizontal_displacement_m": [100., -200.],
               "estimated_stopping_height_m": 50.}
    result = diagnostics.ideal_stop_location(state, preview, profile)
    expected = [(radius-50.)*math.sin(angle)+100.*math.cos(angle), -200.]
    assert result["cg_horizontal_position_enu_m"] == pytest.approx(expected)
    assert result["horizontal_error_m"] == pytest.approx(math.hypot(*expected))
    preview["burn_fuel_feasible"] = False
    assert diagnostics.ideal_stop_location(state, preview, profile) is None


def test_exact_landing_event_is_not_interpolated_and_terminal_required(setup):
    run, profile, config = local_record(setup)
    result = diagnostics.analyze_run(run, profile, config)
    assert [row["time_s"] for row in result["rows"]] == [0., .5]
    assert result["rows"][0]["checkpoint_index"] is None
    run["final_state"] = deepcopy(run["final_state"])
    run["final_state"]["propellant_kg"] -= 1.
    with pytest.raises(ValueError, match="missing_terminal_state"):
        diagnostics.analyze_run(run, profile, config)


def test_conflicting_exact_event_and_checkpoint_are_rejected(setup):
    run, profile, config = local_record(setup)
    point = deepcopy(run["recovery_record"]["checkpoints"][0])
    point.update(time_s=0., state=deepcopy(run["events"][0]["state"]))
    point["state"]["propellant_kg"] -= 1.
    run["recovery_record"]["checkpoints"].insert(0, point)
    with pytest.raises(ValueError, match="landing_event_state_mismatch"):
        diagnostics.analyze_run(run, profile, config)


def test_raw_hash_mismatch_fails_before_analysis(tmp_path):
    path = tmp_path/"record.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="hash_mismatch"):
        cli.read_json(path, "0"*64)


def builder_inputs():
    inputs = {"initial_state": {}, "profile": {}, "catch_profile": {}, "reference_study_sha256": "ref",
              "source_sha256": {}, "previous_comparison_sha256": "fin-hash"}
    original = {"schema": "missionos.starship_boostback_comparison.v1", "verification_passed": True,
                "conditions": [{"cutoff_time_s": 200.-offset, "run_file": f"cutoff-minus-{offset:02d}.json", "run_sha256": "run"}
                               for offset in cli.OFFSETS]}
    coast = {"schema": "missionos.starship_coast_fin_comparison.v1", "verification_passed": True,
             "conditions": [{"cutoff_time_s": 200.-offset, "offset_s": offset, "landing_request_states_equal": True,
               "methods": {role: {"run_file": f"{role}-minus-{offset:02d}.json.gz", "run_sha256": "run",
                                  "uncompressed_run_sha256": "expanded"} for role in ("baseline", "candidate")}}
                            for offset in cli.OFFSETS]}
    return {"original/comparison.json": (original, "original-hash", ""),
            "coast/comparison.json": (coast, "coast-hash", ""),
            "coast/inputs.json": (inputs, "", ""), "original/inputs.json": (deepcopy(inputs), "", ""),
            "fin/comparison.json": ({"schema": "missionos.starship_fin_comparison.v1", "verification_passed": True}, "fin-hash", ""),
            "fin/inputs.json": ({**deepcopy(inputs), "prior_comparison_sha256": "original-hash"}, "", "")}


@pytest.mark.parametrize("fault,expected", [("lineage", "comparison_lineage_mismatch"),
    ("coast_lineage", "comparison_lineage_mismatch"),
    ("cutoff", "unmatched_cutoff_conditions"), ("expanded_hash", "expanded_source_hash_mismatch"),
    ("verification", "source_record_verification_failed"), ("landing_state", "landing_start_states_differ")])
def test_builder_refuses_unmatched_or_unverified_inputs(monkeypatch, fault, expected):
    files = builder_inputs()
    if fault == "lineage":
        files["fin/inputs.json"][0]["prior_comparison_sha256"] = "wrong"
    if fault == "coast_lineage":
        files["coast/inputs.json"][0]["previous_comparison_sha256"] = "wrong"
    if fault == "cutoff":
        files["coast/comparison.json"][0]["conditions"][0]["cutoff_time_s"] += 1
    def read(path, expected_hash=None):
        if str(path) in files:
            return files[str(path)]
        changed = fault == "landing_state" and path.name.startswith("candidate")
        return {"run": {"events": [{"event": "landing_stage_requested", "state": {"fuel": 1 if changed else 0}}]},
                "catch_run": None}, "run", "bad" if fault == "expanded_hash" else "expanded"
    monkeypatch.setattr(cli, "read_json", read)
    monkeypatch.setattr(cli, "verify_recovery", lambda *a, **kw: {"passed": fault != "verification", "issues": []})
    monkeypatch.setattr(cli, "analyze_run", lambda *a: {})
    with pytest.raises(ValueError, match=expected):
        cli.build(Path("original"), Path("coast"), Path("fin"))


def test_failed_cli_is_retained_and_cannot_overwrite_attempt(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("rejected_input")
    monkeypatch.setattr(cli, "build", fail)
    args = ["--baseline-dir", "original", "--coast-fin-dir", "coast", "--fin-dir", "fin", "--output-dir", str(tmp_path)]
    with pytest.raises(ValueError, match="rejected_input"):
        cli.main(args)
    assert json.loads((tmp_path/"failure.json").read_text())["exception"] == "ValueError"
    assert (tmp_path/"attempt.json").exists() and not (tmp_path/"diagnostics.json").exists()
    with pytest.raises(SystemExit):
        cli.main(args)


def test_no_integrator_or_provider_import_and_display_boundaries():
    for module in (diagnostics, cli):
        source = inspect.getsource(module)
        assert "simulate_recovery" not in source and "simulate_catch" not in source
        assert "starship_sixdof import" not in source and "deepseek" not in source
    page = rendering.report({"conditions": []})
    assert "保存状態の診断" in page and "正味の減速と呼びません" in page
    assert "current" not in page  # no current-state HTTP lookup during replay
