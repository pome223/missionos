"""New scope cannot broaden the old scope; causal image and recovery barriers."""

import io
import json
import math
from types import SimpleNamespace

from PIL import Image
import pytest

from scripts import yokohama_visual_gust_contract as contract
from scripts.yokohama_endpoint_feedback import FIXED, feedback_policy
from scripts.yokohama_candidate_recovery import LIMITS, policy, map_clearance
from scripts.yokohama_sitl import write_endpoint_feedback_plan, REPO
from src.runtime.yokohama_visual_input import (
    PROMPT,
    FiniteObservation,
    cpu_color_double,
    raw_action,
)


def config(tmp_path):
    root = tmp_path / "plan"
    args = SimpleNamespace(
        output_dir=root,
        timeout_seconds=900,
        decision_backend="fixture",
        goal_distance_adapter=False,
    )
    write_endpoint_feedback_plan(args)
    c = json.loads((root / "config.json").read_text())
    c["operator_approval_manifest_sha256"] = "a" * 64
    return contract.build_config(c, "reach")


def image_payload(blocked=False):
    image = Image.new("RGB", (640, 360), "gray")
    if blocked:
        image.paste((240, 0, 0), (200, 100, 440, 260))
    b = io.BytesIO()
    image.save(b, format="PNG")
    return dict(prompt=PROMPT, images={"rgb": b.getvalue(), "down": b.getvalue()})


def test_actual_map_eight_metre_corridor_includes_landing_volume(tmp_path):
    c = config(tmp_path)
    assert policy(c) == LIMITS and LIMITS["recovery_timeout_s"] == 220
    p = feedback_policy(c)
    assert p["max_cycles"] == 4 and FIXED["max_cycles"] == 2
    result = map_clearance(
        c, REPO / "docs/examples/yokohama-urban-scene/collision-footprints.geojson"
    )
    assert result["minimum_clearance_m"] > result["required_centerline_clearance_m"]


def test_route_aligned_spawn_is_world_ENU_yaw_and_leaves_old_plan_east(tmp_path):
    c = config(tmp_path)
    heading = math.radians(c["simulator_initial_heading_deg"])
    assert math.remainder(
        math.pi / 2 - contract.spawn_yaw(c) - heading, 2 * math.pi
    ) == pytest.approx(0)
    assert c["simulator_initial_heading_deg"] == pytest.approx(
        c["flight_stages"][0]["items"][-1]["param4"]
    )
    p = c["decisions"]["endpoint_feedback"]
    yaw = contract.spawn_yaw(c)
    a, b = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
    assert [math.cos(yaw), math.sin(yaw)] == pytest.approx([(b[i] - a[i]) / 8 for i in (0, 1)])
    original = json.loads((tmp_path / "plan/config.json").read_text())
    assert original.get("simulator_initial_heading_deg", 90) == 90
    assert all(
        item["param4"] == c["simulator_initial_heading_deg"]
        for item in c["flight_stages"][0]["items"]
    )
    c["simulator_initial_heading_deg"] = 90
    with pytest.raises(ValueError, match="align"):
        contract.validate_config(c)


@pytest.mark.parametrize("fault", [None, "replay", "heading", "moving", "stale", "reset"])
def test_initial_alignment_requires_distinct_ground_stationarity(tmp_path, fault):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)
    yaw = contract.spawn_yaw(c)
    check = contract.initial_alignment_check(c)
    rows = []
    for t in (0, 1, 2):
        r = row(t, [0, 0, 0.2], land=True)
        r.update(
            run_id=c["run_id"],
            world_sha256=c["world"]["world_sha256"],
            heading_ned_rad=math.radians(c["simulator_initial_heading_deg"]),
        )
        r["vehicle"]["quat_wxyz"] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
        if fault == "replay":
            r["vehicle"]["sensor_sim_s"] = 0
        if fault == "heading":
            r["heading_ned_rad"] += 0.04
        if fault == "moving":
            r["velocity_ned"] = [0.31, 0, 0]
        if fault == "stale":
            r["vehicle"]["age_s"] = 2.1
        if fault == "reset":
            r["reset_counters"][2] = t
        rows.append(check(r))
    assert rows[-1] is (fault is None)


def test_calm_catalog_cannot_admit_a_pulse(tmp_path):
    c = config(tmp_path)
    c["visual_gust_trial"]["scenario"] = "calm"
    assert contract.validate_config(c)
    c["wind_mps"] = 0.5
    with pytest.raises(ValueError, match="Calm"):
        contract.validate_config(c)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["decisions"].update(backend="native"),
        lambda c: c["decisions"]["endpoint_feedback"].update(max_cycles=5),
        lambda c: c["decisions"]["endpoint_feedback"].update(corridor_half_width_m=0.6),
        lambda c: c["decisions"]["endpoint_feedback"].update(goal_tolerance_m=0.6),
        lambda c: c["decisions"]["endpoint_feedback"]["goal_world_xyz_m"].__setitem__(0, 99),
        lambda c: c["visual_gust_trial"]["contract"].update(gust_duration_sim_s=3),
        lambda c: c["candidate_recovery"].update(recovery_timeout_s=221),
        lambda c: c.update(delivery_trial={"extra": True}),
        lambda c: c.update(timeout_s=901),
        lambda c: c["world"].update(dynamic_actors={"extra": True}),
    ],
)
def test_new_scope_tampering_rejected(tmp_path, mutation):
    c = config(tmp_path)
    mutation(c)
    with pytest.raises(ValueError):
        feedback_policy(c)


def test_eight_metres_cannot_be_smuggled_into_old_contract(tmp_path):
    c = config(tmp_path)
    c.pop("visual_gust_trial")
    with pytest.raises(ValueError):
        feedback_policy(c)


def test_more_than_two_cycles_requires_matching_observed_predecessor(tmp_path):
    c = config(tmp_path)
    # The actual state validator must reject a missing predecessor before any
    # host/vehicle operation. No fixture bypass of observed pose is admitted.
    from scripts.yokohama_visual_gust_contract import validate_previous

    with pytest.raises(ValueError, match="prior stable"):
        validate_previous(c, dict(cycle=3, observation={}, previous_segment=None))
    with pytest.raises(ValueError, match="cycle"):
        validate_previous(c, dict(cycle=5, observation={}))


def test_RGB_changes_raw_proposal_before_Rules_using_declared_CPU_double():
    a = FiniteObservation(0).observe(0, 0, image_payload(False))
    b = FiniteObservation(0).observe(0, 0, image_payload(True))
    assert a["raw_candidate_before_Rules"]["kind"] == "translation"
    assert b["raw_candidate_before_Rules"]["kind"] == "hold"
    assert a["raw_response"]["vla_inference_invoked"] is False
    assert b["rules_evaluated"] is False and b["dispatch_authorized"] is False


@pytest.mark.parametrize(
    "extra", ["case", "obstacle_xyz", "script", "future_rgb", "expected_action", "goal_distance"]
)
def test_truth_and_expected_action_cannot_enter_model_interface(extra):
    payload = image_payload()
    payload[extra] = "forbidden"
    with pytest.raises(ValueError, match="leakage"):
        cpu_color_double(payload)


def test_reobservation_wait_is_finite_and_never_grants_authority():
    state = FiniteObservation(0)
    records = [state.observe(t, t, image_payload(True)) for t in (0, 1, 2)]
    assert records[-1]["outcome"] == "stop_without_authority"
    assert sum(r["reobservation_allowed"] for r in records) == 2
    with pytest.raises(ValueError):
        state.observe(3, 3, image_payload(True))


@pytest.mark.parametrize("stamp,now", [(0, 3), (5, 4), (21, 21), (float("nan"), 0)])
def test_stale_future_expired_and_nonfinite_image_rejected(stamp, now):
    with pytest.raises(ValueError):
        FiniteObservation(0).observe(stamp, now, image_payload())


def test_old_image_replay_and_after_stop_response_rejected():
    state = FiniteObservation(0)
    state.observe(1, 1, image_payload(True))
    with pytest.raises(ValueError):
        state.observe(1, 1.5, image_payload(True))
    with pytest.raises(ValueError):
        state.observe(2, 2, image_payload())


def test_late_adapter_response_closes_without_candidate_or_retry():
    clock = iter((0, 1, 4))
    state = FiniteObservation(0, clock=lambda: next(clock))
    with pytest.raises(ValueError, match="finite lease"):
        state.observe(0, 0, image_payload())
    assert state.closed
    with pytest.raises(ValueError):
        state.observe(1, 1, image_payload())


def test_invalid_raw_response_closes_the_finite_contract():
    state = FiniteObservation(0)
    with pytest.raises(ValueError):
        state.observe(0, 0, image_payload(), adapter=lambda p: {"generated_text": "LAND"})
    assert state.closed


def test_terminal_predicate_needs_distinct_pose_and_elapsed_stability(tmp_path):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)

    def observation(t):
        r = row(t, [0, 0, 0.2], land=True)
        r.update(run_id=c["run_id"], world_sha256=c["world"]["world_sha256"])
        return r

    check = contract.distinct_landing_predicate(c, observation(0))
    assert not check(observation(0))
    repeated = observation(2)
    repeated["vehicle"]["sensor_sim_s"] = 0
    assert not check(repeated)
    assert not check(observation(1))
    assert check(observation(2))


@pytest.mark.parametrize("fault", [None, "replay", "moving", "altitude", "not_loiter"])
def test_return_hold_waits_for_actual_convergence_not_just_three_seconds(tmp_path, fault):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)
    home = c["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]

    def observation(t, offset):
        r = row(t, [offset, 0, home[2]])
        r.update(run_id=c["run_id"], world_sha256=c["world"]["world_sha256"], nav_state=4)
        return r

    check = contract.return_hold_ready(c, observation(0, 0.5))
    assert not check(observation(0, 0.5))
    assert not check(observation(3, 0.394))
    outcomes = []
    for t in (4, 5, 6):
        r = observation(t, 0.20)
        if fault == "replay":
            r["vehicle"]["sensor_sim_s"] = 4
        if fault == "moving":
            r["velocity_ned"] = [0.31, 0, 0]
        if fault == "altitude":
            r["vehicle"]["xyz"][2] += 0.16
        if fault == "not_loiter":
            r["nav_state"] = 3
        outcomes.append(check(r))
    assert outcomes[-1] is (fault is None)


def test_return_hold_has_minimum_three_seconds_and_rejects_stale_or_reset(tmp_path):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)
    home = c["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]

    def observation(t):
        r = row(t, home.copy())
        r.update(run_id=c["run_id"], world_sha256=c["world"]["world_sha256"], nav_state=4)
        return r

    check = contract.return_hold_ready(c, observation(0))
    for t in (0, 1, 2):
        assert not check(observation(t))
    assert check(observation(3))
    for fault in ("age", "reset"):
        r = observation(4)
        if fault == "age":
            r["vehicle"]["age_s"] = 2.1
        else:
            r["reset_counters"][0] += 1
        with pytest.raises(ValueError):
            check(r)


def test_terminal_predicate_resets_after_a_gap_and_rejects_stale_pose(tmp_path):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)

    def observation(t):
        r = row(t, [0, 0, 0.2], land=True)
        r.update(run_id=c["run_id"], world_sha256=c["world"]["world_sha256"])
        return r

    check = contract.distinct_landing_predicate(c, observation(0))
    assert not check(observation(0))
    assert not check(observation(1))
    assert not check(observation(4))
    stale = observation(5)
    stale["vehicle"]["age_s"] = 3
    with pytest.raises(ValueError):
        check(stale)


@pytest.mark.parametrize("action", ["LAND", "0 49 53", "99 49 49", "43 60 49", "43 49 99"])
def test_visual_wait_contract_does_not_admit_land_rotation_or_unbounded_motion(action):
    response = cpu_color_double(image_payload())
    response["generated_text"] = action
    with pytest.raises(ValueError):
        raw_action(response)


def test_existing_motion_parser_still_rejects_hold():
    from src.runtime.yokohama_native import vla_candidate

    with pytest.raises(ValueError):
        vla_candidate("0 49 49", {})


@pytest.mark.parametrize("fault", [None, "unconfirmed", "drift", "duplicate"])
def test_pulse_transport_and_finite_settling_guards(tmp_path, monkeypatch, fault):
    from tests.test_yokohama_candidate_recovery import row

    c = config(tmp_path)
    home = c["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]
    current = [0]
    transports, events = [], []

    def observation(t):
        r = row(t, list(home))
        r.update(
            run_id=c["run_id"],
            world_sha256=c["world"]["world_sha256"],
            heading_ned_rad=0,
            nav_state=4,
            phase="00-D1",
        )
        r["vehicle"]["quat_wxyz"] = [1, 0, 0, 0]
        if fault == "drift" and len(transports) == 2:
            r["vehicle"]["xyz"][0] += 0.6
        return r

    def activate(velocity):
        transports.append(velocity)
        return dict(
            confirmed=fault != "unconfirmed",
            requested_enu_mps=velocity,
            start_sim_s=current[0],
            end_sim_s=current[0],
        )

    def wait_for(predicate, timeout):
        assert timeout in (15, 20)
        for _ in range(8):
            if fault != "duplicate" or len(transports) != 2:
                current[0] += 1
            r = observation(current[0])
            if predicate(r):
                return r
        raise TimeoutError("Finite mocked observation window exhausted")

    monkeypatch.setattr(contract, "Path", lambda _: tmp_path)

    def call():
        return contract.pulse(
            c,
            SimpleNamespace(activate_wind=activate),
            lambda: observation(current[0]),
            wait_for,
            lambda name, **data: events.append(name),
            lambda: current[0],
            lambda: None,
        )

    if fault:
        with pytest.raises((ValueError, TimeoutError)):
            call()
        assert not (tmp_path / "weak-gust.json").exists()
    else:
        final = call()
        assert transports == [[0.5, 0, 0], [0, 0, 0]]
        record = json.loads((tmp_path / "weak-gust.json").read_text())
        assert record["pulse_count"] == 1 and record["stable_samples"] == 3
        assert final["sim_s"] == 5
        assert events[-1] == "weak_gust_settled_before_reobservation"


def test_CLI_rejects_new_native_and_extra_wind_before_any_subprocess(tmp_path, monkeypatch):
    from scripts import yokohama_sitl as sitl

    approval = tmp_path / "approval.json"
    approval.write_text("{}")
    args = contract.arguments("sha256:" + "a" * 64, "reach") + [
        "--approve-sitl",
        "--approval-manifest",
        str(approval),
        "--output-dir",
        str(tmp_path / "run"),
    ]
    monkeypatch.setattr(
        sitl.subprocess, "run", lambda *a, **k: pytest.fail("No subprocess admitted")
    )
    for extra in (["--decision-backend", "native"], ["--wind-east-mps", "1"], ["--sea-round-trip"]):
        with pytest.raises(SystemExit):
            sitl.main(args + extra)
