"""Choice headroom, observation boundaries, and urban runtime rejection tests."""

from copy import deepcopy

import pytest
from click.testing import CliRunner

from missionos_cli.cli import missionos
from src.runtime.ship_delivery import ShipDeliveryScenario
from src.runtime.ship_delivery_sitl import build_ship_sitl_missions
from src.runtime.ship_urban_decision import choose_urban_action, screen_urban_decisions
from src.runtime.ship_urban_world import configure_urban, urbanize_missions, verify_urban_run
from src.runtime.ship_urban_world import segment_intersects_box


def history(speed):
    return [{"observed_at_s": t, "obstacle_x_m": t * speed} for t in (1, 2, 3)]


def test_screen_discriminates_actions_without_claiming_model_headroom():
    result = screen_urban_decisions()
    assert [r["offline_oracle_action"] for r in result["cases"]] == ["wait", "detour"]
    assert result["adaptive_gain_over_best_fixed_s"] > 5
    assert result["oracle_headroom_over_constant_velocity_s"] == 0
    assert not result["learned_model_comparison_admitted"]
    assert not result["px4_runtime_invoked"]


@pytest.mark.parametrize(
    "speed,action", [(2, "wait"), (0.15, "detour"), (0, "detour"), (-1, "detour")]
)
def test_velocity_rule_uses_observed_motion(speed, action):
    assert choose_urban_action(history(speed), "constant_velocity")["action"] == action


@pytest.mark.parametrize("mutation", ["future", "nan", "clock", "gap", "short", "oracle"])
def test_unqualified_policy_inputs_fail_closed(mutation):
    rows = history(2)
    policy = "constant_velocity"
    if mutation == "future":
        rows[-1]["clear_at_s"] = 4
    elif mutation == "nan":
        rows[-1]["obstacle_x_m"] = float("nan")
    elif mutation == "clock":
        rows[-1]["observed_at_s"] = 1
    elif mutation == "gap":
        rows[-1]["observed_at_s"] = 10
    elif mutation == "short":
        rows = rows[:2]
    else:
        policy = "oracle"
    with pytest.raises(ValueError):
        choose_urban_action(rows, policy)


def test_urban_routes_have_indefinite_entry_and_common_return_detour():
    scenario = ShipDeliveryScenario(offshore_distance_m=100)
    urban = configure_urban(scenario, "short_clear", "constant_velocity")
    missions = build_ship_sitl_missions(scenario)
    urbanize_missions(missions, urban)
    assert missions["outbound"][-1]["command"] == 17
    assert missions["urban-wait"][-1]["command"] == 17
    assert missions["urban-detour"][-1]["command"] == 17
    assert max(i["longitude_deg"] for i in missions["urban-detour"]) > 138.744
    assert max(i["longitude_deg"] for i in missions["return"]) > 138.744
    assert missions["return"][-1]["command"] == 21
    for name in ("outbound", "urban-wait", "urban-detour", "return"):
        assert [i["seq"] for i in missions[name]] == list(range(len(missions[name])))
        assert [i["current"] for i in missions[name]] == [1] + [0] * (len(missions[name]) - 1)
    assert not {id(i) for i in missions["urban-wait"]} & {id(i) for i in missions["urban-detour"]}


def evidence():
    urban = configure_urban(
        ShipDeliveryScenario(offshore_distance_m=100), "short_clear", "constant_velocity"
    )
    urban["buildings"] = [{"name": "building", "xyz": [45, 145, 22.5], "size": [35, 30, 45]}]
    rows = [
        {
            "elapsed_s": t,
            "urban_obstacle": {"id": 100, "xyz": [t * 2, 170, 30]},
            "poses_fresh": True,
            "urban_contact_monitors_connected": True,
            "urban_contact_observed": False,
            "urban_buildings": [{"id": 200, "xyz": [45, 145, 22.5], "size": [35, 30, 45]}],
            "vehicle": {"id": 55, "xyz": [0, 70 if t < 10 else 300, 30]},
        }
        for t in range(1, 15)
    ]
    decisions = choose_urban_action(history(2), "constant_velocity")
    names = [
        ("urban_entry_observed", 0),
        ("urban_actor_started", 0.1),
        ("urban_decision", 3.1),
        ("urban_upload", 7.5),
        ("urban_ready", 8),
        ("urban_requested", 8.1),
        ("delivery_hover_observed", 14),
    ]
    events = [{"event": n, "elapsed_s": t} for n, t in names]
    events[2].update(history=history(2), decision=decisions)
    events[-2]["action"] = "wait"
    return rows, events, urban


def test_runtime_evidence_requires_clearance_before_departure():
    rows, events, urban = evidence()
    assert verify_urban_run(rows, events, urban)["verified"]


@pytest.mark.parametrize(
    "mutation",
    ["collision", "stale", "missing", "motion", "building", "history", "dispatch", "early"],
)
def test_bad_urban_runtime_evidence_is_rejected(mutation):
    rows, events, urban = deepcopy(evidence())
    if mutation == "collision":
        rows[-1]["urban_contact_observed"] = True
    elif mutation == "stale":
        rows[-1]["urban_contact_monitors_connected"] = False
    elif mutation == "missing":
        events.pop()
    elif mutation == "motion":
        for r in rows:
            r["urban_obstacle"]["xyz"][0] = 0
    elif mutation == "building":
        rows[-1]["urban_buildings"] = []
    elif mutation == "history":
        rows[0]["urban_obstacle"]["xyz"][0] = 99
    elif mutation == "dispatch":
        events[-2]["action"] = "detour"
    else:
        events[-2]["elapsed_s"] = 5
    assert not verify_urban_run(rows, events, urban)["verified"]


def test_screen_cli_runs_without_model_or_docker():
    result = CliRunner().invoke(missionos, ["ship-delivery", "urban-screen"])
    assert result.exit_code == 0, result.output
    assert '"learned_model_comparison_admitted": false' in result.output


def test_swept_clearance_detects_intersection_between_outside_endpoints():
    assert segment_intersects_box([-2, 0, 0], [2, 0, 0], [1, 1, 1])
    assert not segment_intersects_box([-2, 2, 0], [2, 2, 0], [1, 1, 1])


def comparison_rows():
    common = {
        "scenario": {},
        "world_sha256": "world",
        "image_id": "image",
        "worker_sha256": "worker",
        "support_code_sha256": {},
        "verified": True,
        "collision_observed": False,
        "geometric_clearance_verified": True,
        "max_outbound_lateral_m": 85,
    }
    return [
        dict(
            common, run_id=str(i), case=case, policy=policy, action=action, urban_elapsed_s=seconds
        )
        for i, (case, policy, action, seconds) in enumerate(
            [
                ("short_clear", "constant_velocity", "wait", 30),
                ("short_clear", "always_detour", "detour", 50),
                ("long_block", "constant_velocity", "detour", 50),
                ("long_block", "always_wait", "wait", 100),
            ]
        )
    ]


def test_comparison_keeps_zero_rule_headroom_distinct_from_fixed_action_gain():
    from src.runtime.ship_urban_comparison import compare_verified_outcomes

    result = compare_verified_outcomes(comparison_rows())
    assert [r["rule_gain_over_opposite_action_s"] for r in result["cases"]] == [20, 50]
    assert result["mean_observed_headroom_over_rule_s"] == 0
    assert not result["learned_model_comparison_admitted"]


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing", "scenario", "worker", "unverified", "nan", "coverage"]
)
def test_comparison_rejects_unmatched_or_incomplete_runs(mutation):
    from src.runtime.ship_urban_comparison import compare_verified_outcomes

    rows = deepcopy(comparison_rows())
    if mutation == "duplicate":
        rows[-1]["run_id"] = rows[0]["run_id"]
    elif mutation == "missing":
        rows.pop()
    elif mutation == "scenario":
        rows[-1]["scenario"] = {"wind_mps": 20}
    elif mutation == "worker":
        rows[-1]["support_code_sha256"] = {"stage": "changed"}
    elif mutation == "unverified":
        rows[-1]["verified"] = False
    elif mutation == "nan":
        rows[-1]["urban_elapsed_s"] = float("nan")
    else:
        rows[-1]["action"] = "detour"
    with pytest.raises(ValueError):
        compare_verified_outcomes(rows)


@pytest.mark.parametrize(
    "mutation", ["missing_vehicle", "stale_pose", "nan_obstacle", "nan_building"]
)
def test_clearance_never_passes_with_unusable_positions(mutation):
    rows, events, urban = deepcopy(evidence())
    if mutation == "missing_vehicle":
        rows[-1]["vehicle"] = None
    elif mutation == "stale_pose":
        rows[-1]["poses_fresh"] = False
    elif mutation == "nan_obstacle":
        rows[-1]["urban_obstacle"]["xyz"][2] = float("nan")
    else:
        rows[-1]["urban_buildings"][0]["xyz"][2] = float("nan")
    assert not verify_urban_run(rows, events, urban)["verified"]
