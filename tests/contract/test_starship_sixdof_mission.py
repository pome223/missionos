"""Mission integration boundaries; physics references live in validation tests."""
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as sd
from src.runtime.starship_sixdof_mission import _attitude, _split, bound_orbit_above, control, simulate, stack_vehicle, vehicle

ROOT = Path(__file__).resolve().parents[2]


def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


def test_full_target_frame_has_intended_roll_and_thrust_axes():
    for z, x in [((1, 0, 0), (0, 1, 0)), ((0, 0, -1), (0, -1, 0)), ((.1, .2, .3), (1, 0, 0))]:
        q = _attitude(z, x)
        assert sd.rotate(q, (0, 0, 1)) == pytest.approx(env.unit(z), abs=1e-12)
        assert env.dot(sd.rotate(q, (1, 0, 0)), x) > 0


def test_stage_split_keeps_fault_and_actuator_state_and_conserves_momentum():
    p = profile()
    ship, booster = vehicle(p), vehicle(p, "booster")
    stack = stack_vehicle(p, ship, booster)
    engines = [sd.EngineState(.7, .01, -.01) if e.max_gimbal_rad else sd.EngineState() for e in stack.engines]
    engines[3] = sd.EngineState(0, 0, 0, available=False)
    s = sd.State6DOF(160., (7e6, 0, 0), (0, 3000, 0), sd.axis_angle((1, 2, 3), .7), (.01, -.02, .03), 260000,
                    tuple(engines), tuple(0. for _ in stack.aero_panels))
    bs, ss, receipt = _split(s, stack, booster, ship, (0, 0, 72), p["ship"]["propellant_kg"])
    assert bs.engine_states[3].available is False
    assert bs.engine_states[0] == s.engine_states[0]
    assert ss.q_body_to_eci == s.q_body_to_eci
    assert ss.omega_body_rad_s == s.omega_body_rad_s
    assert ss.r_eci_m != bs.r_eci_m
    assert ss.v_eci_mps != bs.v_eci_mps  # rotational offset velocity retained
    assert abs(receipt["mass_residual_kg"]) < 1e-6
    assert receipt["linear_momentum_residual_kg_mps"] < .01
    assert receipt["angular_momentum_residual_kg_m2_s"] < .01


def test_attitude_request_does_not_assign_orientation_or_unbounded_torque():
    p = profile()
    v = vehicle(p)
    s = sd.State6DOF(0, (7e6, 0, 0), (0, 0, 0), sd.IDENTITY, (0, 0, 0), 45000,
                    tuple(sd.EngineState() for _ in v.engines), tuple(0. for _ in v.aero_panels))
    target = sd.axis_angle((0, 1, 0), math.pi/2)
    cmd, _ = control(s, v, target, 0, 0, p)
    after = sd.step(s, v, cmd, .1, gravity=False, atmosphere=False)
    assert after.q_body_to_eci != target
    assert env.norm(after.omega_body_rad_s) > 0
    assert after.propellant_kg < s.propellant_kg
    assert env.norm(after.v_eci_mps) < 1e-10  # finite jet couple has zero net force
    assert all(abs(c.gimbal_x_rad) <= e.max_gimbal_rad for c, e in zip(cmd.engines, v.engines))


def test_short_launch_reports_horizon_not_mission_success_and_no_roll_jump():
    run = simulate(profile(), duration_s=3)
    assert run["outcome"]["termination"] == "time_limit"
    assert run["outcome"]["orbit_gate_reached"] is False
    assert run["outcome"]["max_attitude_error_deg"] < 1
    assert run["samples"][-1]["altitude_m"] > run["samples"][0]["altitude_m"]
    assert run["outcome"]["attitude_prescribed"] is False


def test_configured_launch_azimuth_changes_trajectory():
    east = profile()
    north = profile()
    north["launch"]["azimuth_deg"] = 0
    a, b = simulate(east, duration_s=35), simulate(north, duration_s=35)
    assert env.norm(env.add(tuple(a["final_state"]["r_eci_m"]), env.scale(tuple(b["final_state"]["r_eci_m"]), -1))) > 1


def test_hyperbolic_perigee_does_not_authorize_orbit_or_release():
    radius = env.EARTH_EQUATORIAL_RADIUS_M+275000
    circular = math.sqrt(env.EARTH_MU_M3_S2/radius)
    bound = env.orbital_elements(env.State3D(0, (radius, 0, 0), (0, circular, 0), 0))
    escape = env.orbital_elements(env.State3D(0, (radius, 0, 0), (0, 12000, 0), 0))
    assert bound_orbit_above(bound, 220000)
    assert escape["perigee_altitude_m"] > 220000
    assert not bound_orbit_above(escape, 220000)


def test_real_cli_opt_in_artifact_and_manifest(tmp_path):
    script = ROOT/"scripts/run_starship_sixdof.py"
    result = subprocess.run([sys.executable, str(script), "--scenario", "gimbal_step", "--duration-s", "1", "--output-dir", str(tmp_path/"off")], capture_output=True, text=True)
    assert result.returncode == 2 and not (tmp_path/"off").exists()
    out = tmp_path/"approved"
    result = subprocess.run([sys.executable, str(script), "--approve-simulation", "--scenario", "gimbal_step", "--duration-s", "1", "--output-dir", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    study = json.loads((out/"study.json").read_text())
    assert study["runs"][0]["outcome"]["six_dof_integrated"] is True
    assert study["provenance"]["physical_execution_invoked"] is False
    page = (out/"report.html").read_text()
    assert "integrated_quaternion" in page
    assert "attitude_matrix_local" in page
    assert "SpaceX実機の精度を検証したシミュレーターではありません" in page
    assert "report.html" in json.loads((out/"manifest.json").read_text())["files"]


def test_cli_does_not_overwrite_a_failed_partial_run(tmp_path):
    evidence = tmp_path/"failure.json"
    evidence.write_text('{"failed_previous_run": true}')
    result = subprocess.run([sys.executable, str(ROOT/"scripts/run_starship_sixdof.py"), "--approve-simulation", "--duration-s", "1", "--output-dir", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert evidence.read_text() == '{"failed_previous_run": true}'
    assert not (tmp_path/"study.json").exists()


def test_cli_cpu_validation_keeps_native_not_run(tmp_path):
    from src.runtime.starship_sixdof_validation import run_validation
    validation = run_validation(native_basilisk_enabled=False, approve_simulation=True)
    reference = tmp_path/"validation.json"
    reference.write_text(json.dumps(validation))
    out = tmp_path/"run"
    result = subprocess.run([sys.executable, str(ROOT/"scripts/run_starship_sixdof.py"), "--approve-simulation", "--scenario", "gimbal_step", "--duration-s", ".2", "--validation", str(reference), "--output-dir", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    study = json.loads((out/"study.json").read_text())
    native = next(c for c in study["checks"] if c["name"] == "native Basilisk 実呼出し")
    assert native["passed"] is None
    assert (out/"validation.json").read_bytes() == reference.read_bytes()
