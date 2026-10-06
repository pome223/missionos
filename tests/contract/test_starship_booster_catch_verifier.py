"""Independent support-kinematics examples and tamper admission tests."""
from __future__ import annotations
from copy import deepcopy
import ast
import inspect
import json
from pathlib import Path
import pytest
from src.runtime import starship_booster_catch_verifier as verifier

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope="module")
def configuration():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())

@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())

@pytest.fixture(scope="module")
def campaign(profile, configuration):
    from src.runtime.starship_booster_catch import simulate_catch
    # Production fixture exercises the complete boundary once. Tests corrupt
    # its evidence instead of only comparing producer and verifier output.
    return json.loads(json.dumps(simulate_catch(profile, configuration)))

@pytest.fixture
def evidence(campaign):
    return deepcopy(campaign)

def verify(evidence, profile, configuration):
    result = verifier.verify_catch(evidence, profile, configuration)
    assert result["catch_verified"] is result["physical_execution"] is result["mission_completed"] is False
    json.dumps(result, allow_nan=False)
    return result

def reject(evidence, profile, configuration, code=None):
    result = verify(evidence, profile, configuration)
    assert result["passed"] is False, result
    assert result["simulated_catch_supported"] is False
    if code:
        assert result["issues"][0]["code"] == code, result

def test_saved_support_is_valid_but_not_a_real_catch(evidence, profile, configuration):
    result = verify(evidence, profile, configuration)
    assert result["passed"] and result["simulated_catch_supported"], result
    assert result["launch_connected"] is False
    assert result["observed_settle_s"] >= configuration["settle_time_s"]-1e-8

def test_no_producer_or_integrator_imports():
    source = inspect.getsource(verifier)
    relative_imports = {node.module for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.ImportFrom) and node.level}
    assert relative_imports == {"starship_wind_verifier"}
    wind_source = (ROOT/"src/runtime/starship_wind_verifier.py").read_text()
    assert not [node for node in ast.walk(ast.parse(wind_source))
                if isinstance(node, ast.ImportFrom) and node.level]
    assert "import numpy" not in wind_source
    assert "import starship" not in source
    assert "import numpy" not in source

def test_hand_derived_rotating_point_and_moving_centroid(configuration):
    config = deepcopy(configuration)
    config["support_points_body_m"] = [[-6., 0., 80.], [6., 0., 80.]]
    radius, rate = 6378137.+39.9, 7.292115e-5
    # Equatorial body x/east, y/north, z/up. Hull descent is 1 m/s and
    # centroid rises .01 m/s in body axes: material points descend 1.01 m/s.
    frame = {"time_s": 0., "r_eci_m": [radius, 0., 0.], "v_eci_mps": [-1., rate*radius, 0.],
             "q_body_to_eci": [.5, .5, .5, .5], "omega_body_rad_s": [0., rate, 0.],
             "com_body_m": [0., 0., 20.], "com_rate_body_mps": [0., 0., .01],
             "arm_half_span_m": 6., "arm_rate_mps": 0., "pins": [],
             "force_body_n": [0., 0., 5_232_000.], "torque_body_nm": [0., 0., 0.]}
    for i, sign in enumerate((-1., 1.)):
        frame["pins"].append({"id": i, "position_body_m": [sign*6., 0., 80.],
            "position_enu_m": [sign*6., 0., 99.9], "relative_velocity_enu_mps": [0., 0., -1.01],
            "arm_velocity_enu_mps": [0., 0., 0.], "footprint_active": True,
            "top_contact_eligible": True, "penetration_m": .1,
            "normal_force_n": 2_616_000., "force_enu_n": [0., 0., 2_616_000.]})
    p = {"launch": {"latitude_deg": 0., "longitude_deg": 0.}}
    loads, compression, speeds, armed = verifier._contact(frame, p, config, 2., True, False, [True, True])
    assert loads == pytest.approx([2_616_000., 2_616_000.], abs=.02)
    assert compression == pytest.approx([.1, .1])
    assert speeds == pytest.approx([1.01, 1.01])
    assert armed == [True, True]
    frame["com_rate_body_mps"][2] = 0.
    with pytest.raises(verifier._Invalid):
        verifier._contact(frame, p, config, 2., True, False, [True, True])

@pytest.mark.parametrize("scenario", ["tower_unavailable", "lateral_offset", "fast_descent", "one_support"])
def test_honest_negative_is_valid_evidence(profile, configuration, scenario):
    from src.runtime.starship_booster_catch import simulate_catch
    run = json.loads(json.dumps(simulate_catch(profile, configuration, scenario)))
    result = verify(run, profile, configuration)
    assert result["passed"] and not result["simulated_catch_supported"], result

@pytest.mark.parametrize("key", ["catch_verified", "physical_execution", "physical_execution_invoked",
                                "mission_completed", "starship_vehicle_validated", "landing_hardware_validated"])
def test_unsupported_claim_is_rejected(evidence, profile, configuration, key):
    evidence["outcome"][key] = True
    reject(evidence, profile, configuration, "claim_boundary")

@pytest.mark.parametrize("key", ["normal_stiffness_npm", "support_height_m", "settle_pin_speed_mps"])
def test_config_is_separately_bound(evidence, profile, configuration, key):
    evidence["catch_record"]["configuration"][key] *= 2
    reject(evidence, profile, configuration, "configuration_binding")

@pytest.mark.parametrize("path,value", [
    (("catch_record", "initialization", "launch_connected"), True),
    (("catch_record", "eligibility", "catch_authorized"), False),
    (("catch_record", "eligibility", "tower_ready"), False),
    (("catch_record", "missing_support"), True),
    (("outcome", "simulated_catch_supported"), False),
    (("outcome", "orbit_gate_reached"), True),
    (("outcome", "payload_released_count"), 1),
    (("outcome", "termination"), "time_limit"),
    (("outcome", "attitude_prescribed"), True),
    (("catch_record", "settling", "observed_s"), 99.),
    (("catch_record", "peak_support_force_n"), 0.),
])
def test_false_summary_rejected(evidence, profile, configuration, path, value):
    target = evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    reject(evidence, profile, configuration)

@pytest.mark.parametrize("field", ["position_enu_m", "relative_velocity_enu_mps", "arm_velocity_enu_mps", "force_enu_n"])
def test_point_vectors_are_recomputed(evidence, profile, configuration, field):
    evidence["catch_record"]["frames"][400]["pins"][0][field][0] += 1.
    reject(evidence, profile, configuration)

@pytest.mark.parametrize("field", ["normal_force_n", "penetration_m"])
def test_contact_load_is_recomputed(evidence, profile, configuration, field):
    evidence["catch_record"]["frames"][400]["pins"][0][field] += 10.
    reject(evidence, profile, configuration)

@pytest.mark.parametrize("field", ["force_body_n", "torque_body_nm"])
def test_resultant_load_is_recomputed(evidence, profile, configuration, field):
    evidence["catch_record"]["frames"][400][field][0] += 100.
    reject(evidence, profile, configuration)

@pytest.mark.parametrize("field", ["footprint_active", "top_contact_eligible"])
def test_point_contact_flags_cannot_override_geometry(evidence, profile, configuration, field):
    frame = evidence["catch_record"]["frames"][400]
    frame["pins"][0][field] = not frame["pins"][0][field]
    reject(evidence, profile, configuration)

@pytest.mark.parametrize("field,delta", [("sampled_at_s", -1.), ("sampled_at_s", 1.), ("valid_until_s", -1.), ("valid_until_s", 1.)])
def test_stale_future_expired_or_unbounded_health_rejected(evidence, profile, configuration, field, delta):
    evidence["catch_record"]["frames"][1]["eligibility"][field] += delta
    reject(evidence, profile, configuration, "readiness_freshness")

@pytest.mark.parametrize("field", ["rules_catch_allowed", "tower_ready", "vehicle_ready"])
def test_current_readiness_cannot_disagree_with_rules(evidence, profile, configuration, field):
    evidence["catch_record"]["frames"][1]["eligibility"][field] = False
    reject(evidence, profile, configuration, "eligibility")

def test_arm_motion_is_not_a_pose_snap(evidence, profile, configuration):
    evidence["catch_record"]["frames"][1]["arm_half_span_m"] = configuration["arm_closed_half_span_m"]
    reject(evidence, profile, configuration, "arm_motion")

def test_position_teleport_rejected(evidence, profile, configuration):
    evidence["catch_record"]["frames"][1]["r_eci_m"][0] += 10.
    reject(evidence, profile, configuration)

def test_velocity_weld_rejected(evidence, profile, configuration):
    evidence["catch_record"]["frames"][400]["v_eci_mps"] = [0., 0., 0.]
    reject(evidence, profile, configuration)

def test_attitude_reset_rejected(evidence, profile, configuration):
    evidence["catch_record"]["frames"][1]["q_body_to_eci"] = [1., 0., 0., 0.]
    reject(evidence, profile, configuration)

def test_duplicate_time_cannot_earn_stable_support(evidence, profile, configuration):
    frame = evidence["catch_record"]["frames"][600]
    frame["time_s"] = evidence["catch_record"]["frames"][599]["time_s"]
    frame["eligibility"]["sampled_at_s"] = frame["time_s"]
    frame["eligibility"]["valid_until_s"] = frame["time_s"]+.05
    reject(evidence, profile, configuration, "time_order")

@pytest.mark.parametrize("field", ["settle_eligible", "settle_elapsed_s"])
def test_settling_is_recomputed(evidence, profile, configuration, field):
    frame = evidence["catch_record"]["frames"][1]
    frame[field] = True if field == "settle_eligible" else 2.
    reject(evidence, profile, configuration, "settling")

@pytest.mark.parametrize("binding", ["initial_state", "final_state"])
def test_initial_and_final_states_are_bound(evidence, profile, configuration, binding):
    evidence[binding]["v_eci_mps"][0] += .1
    reject(evidence, profile, configuration)

def test_false_terminal_event_rejected(evidence, profile, configuration):
    evidence["events"][-1]["event"] = "mission_complete"
    reject(evidence, profile, configuration, "events")

@pytest.mark.parametrize("value", [None, [], True, float("nan"), {"x": float("inf")}])
def test_malformed_root_fails_closed(profile, configuration, value):
    reject(value, profile, configuration)

def test_cyclic_data_fails_closed(profile, configuration):
    data = {}
    data["cycle"] = data
    reject(data, profile, configuration, "invalid_json")
