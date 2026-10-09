"""Delivery admission, immutable old limits and pre-dispatch recovery barriers."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import yokohama_delivery_contract as contract
from scripts import yokohama_sitl as sitl
from scripts.yokohama_candidate_recovery import LIMITS
from scripts.yokohama_delivery_recovery import recover, validate_sample
from scripts.yokohama_endpoint_feedback import FIXED, feedback_policy
from scripts.yokohama_flight_worker import ap_exit_connector
from scripts.yokohama_goal_distance_adapter import POLICY
from src.runtime import yokohama_execution_service as vehicle

IMAGE = "sha256:" + "a" * 64


def base_config(backend="fixture", scenario="delivery"):
    points = {"D1": [0, 0, 15], "D2": [70, 0, 15], "D3": [100, 50, 15],
              "DELIVERY": [150, 50, 15]}
    coast, ship, hover = [400, 0, 15], [1400, 0, 15], [150, 50, 2]
    targets = [ship, coast, points["D1"], points["D2"], points["D3"], points["DELIVERY"],
               hover, points["DELIVERY"], points["D3"], points["D2"], points["D1"], coast, ship]
    names = [n for n in contract.STAGES if n != "01-FEEDBACK-EXIT"]
    return dict(
        run_id="inert-delivery", timeout_s=2100, operator_approval_manifest_sha256="a" * 64,
        hold_duration_sim_s=30, hold_horizontal_tolerance_m=1, hold_vertical_tolerance_m=0.6,
        hold_max_speed_mps=0.5,
        world=dict(world_sha256="a" * 64, source_sha256={"collision-footprints.geojson": "b" * 64},
                   points=[dict(id=n, world_xyz_m=p) for n, p in points.items()],
                   payload_delivery=dict(hover_world_xyz_m=hover, attachment_offset_z_m=-0.14,
                                         removable_support_entity="delivery_cargo_support"),
                   **({"pad_queue": {"fixture": True}} if scenario == "wait" else {}),
                   sea_extension=dict(stationary_ship=True, wind_mps=0, offshore_distance_m=1000,
                                      coast_to_city_entry_m=400, sea_airspeed_mps=8, city_airspeed_mps=3,
                                      coast_world_xyz_m=coast, ship_hold_world_xyz_m=ship,
                                      ship_deck_world_xyz_m=[1400, 0, 0])),
        decisions=dict(backend=backend, wam_profile="motion-v4", points=["D1", "D2"],
                       sea_leg_present=True, payload_release_present=True, fixture_delay_s={}),
        flight_stages=[dict(name=n, target_world_xyz_m=t, airspeed_mps=8 if n.startswith("SEA-") else 3,
                           items=[dict(seq=0, command=17, current=1, param4=0)])
                       for n, t in zip(names, targets)],
    )


def config(backend="fixture", scenario="delivery"):
    return contract.build_config(base_config(backend, scenario), scenario)


def proposal(root, backend="fixture", scenario="delivery", service=None):
    p = dict(schema="yokohama.delivery-feedback-proposal.v1", proposal_id="inert-test",
             contract=deepcopy(contract.CONTRACT), feedback_limits=deepcopy(FIXED),
             adapter_policy=deepcopy(POLICY), physical_execution_invoked=False,
             image_id=IMAGE, city_models=backend, scenario=scenario,
             native_service_config=str(service) if service else None,
             native_service_config_sha256=hashlib.sha256(service.read_bytes()).hexdigest() if service else None,
             input_sha256=vehicle.recovery_input_hashes(root),
             simulator_arguments=contract.arguments(backend, IMAGE, scenario, service))
    return dict(proposal=p, approval=dict(approved_proposal_sha256=vehicle.proposal_digest(p),
                maximum_actual_flight_trials=1, operator_approval_ref="inert",
                actor_session_id="inert", approved_at="2026-10-09T00:00:00Z"))


@pytest.mark.parametrize("scenario", contract.SCENARIOS)
def test_full_route_keeps_original_four_metre_limits_and_model_free_exit(scenario):
    c = config(scenario=scenario)
    p = feedback_policy(c)
    assert all(p[k] == v for k, v in FIXED.items())
    assert [s["name"] for s in c["flight_stages"]] == list(contract.STAGES)
    assert ap_exit_connector(c, {"connector_name": "arbitrary-dynamic-connector"}) is None
    assert LIMITS["recovery_timeout_s"] == 220
    assert c["decisions"]["points"] == ["D1"]


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(timeout_s=2101),
    lambda c: c["delivery_trial"].update(recovery_timeout_s=501),
    lambda c: c.update(hold_duration_sim_s=29),
    lambda c: c.update(hold_horizontal_tolerance_m=1),
    lambda c: c["decisions"]["endpoint_feedback"].update(max_vla_requests=3),
    lambda c: c["decisions"]["endpoint_feedback"].update(goal_tolerance_m=1),
    lambda c: c["flight_stages"][5]["target_world_xyz_m"].__setitem__(0, 999),
    lambda c: c["world"]["sea_extension"].update(ship_deck_world_xyz_m=[0, 0, 0]),
    lambda c: c["flight_stages"][0].update(airspeed_mps=9),
    lambda c: c["decisions"].update(fixture_delay_s={"vla": 10}),
    lambda c: c.update(candidate_recovery=deepcopy(LIMITS)),
])
def test_route_budget_or_threshold_tampering_fails_closed(mutation):
    c = config()
    mutation(c)
    with pytest.raises(ValueError):
        feedback_policy(c)


def test_exact_source_bound_admission_and_service_change(tmp_path):
    service = tmp_path / "service.json"
    service.write_text('{"vla_port":18117,"wam_port":18118}')
    p = proposal(sitl.REPO, "native", service=service)
    contract.admit(json.dumps(p).encode(), sitl.REPO, p["proposal"]["simulator_arguments"])
    service.write_text("changed")
    with pytest.raises(ValueError, match="changed since approval"):
        contract.admit(json.dumps(p).encode(), sitl.REPO, p["proposal"]["simulator_arguments"])


@pytest.mark.parametrize("scenario", ["wait", "candidate_rejected", "timeout"])
def test_fault_and_wait_catalogs_cannot_start_native_models(scenario):
    with pytest.raises(ValueError):
        contract.arguments("native", IMAGE, scenario, "inert.json")


def test_cli_replay_and_extra_flags_have_no_runtime_side_effect(tmp_path, monkeypatch):
    p = proposal(sitl.REPO)
    approval = tmp_path / "execution-approval.json"
    approval.write_text(json.dumps(p))
    (tmp_path / "flight-attempt-claimed.json").write_text("claimed")
    monkeypatch.setattr(sitl.subprocess, "run", lambda *a, **kw: pytest.fail("No runtime allowed"))
    args = p["proposal"]["simulator_arguments"] + ["--approve-sitl", "--approval-manifest",
                                                  str(approval), "--output-dir", str(tmp_path / "run")]
    with pytest.raises(SystemExit):
        sitl.main(args + ["--wind-east-mps", "0.1"])
    with pytest.raises(FileExistsError):
        sitl.main(args)


def test_ship_recovery_never_uploads_without_full_fixed_reserve():
    calls = []
    c = config()
    decisions = SimpleNamespace()
    with pytest.raises(ValueError, match="fixed ship-return reserve"):
        recover(c, decisions, ValueError("rejected"), clock=lambda: 1600,
                sample=lambda: pytest.fail("No sample after admission failure"),
                upload=lambda *a: calls.append(a), activate=lambda **k: calls.append(k),
                wait_for=None, land=None, contacts=None, event=None, set_phase=None, set_speed=None)
    assert calls == []


def test_unknown_recovery_phase_cannot_enter_ship_corridor():
    with pytest.raises(ValueError, match="recovery phase"):
        validate_sample(config(), {"vehicle": {"xyz": [1400, 0, 15]}, "phase": "unapproved"}, {})


def test_offline_verifier_rejects_coordinated_source_and_world_waypoint_change():
    from scripts.verify_yokohama_delivery_trial import verify_authored_route
    world = dict(points=[dict(id="D1", xyz_m=[1, 2, 3], world_xyz_m=[1, 2, 3])],
                 frame=dict(source_origin_xyz_m=[0, 0, 0], source_to_world_matrix=
                            [[1, 0, 0], [0, 1, 0], [0, 0, 1]]))
    route = dict(waypoints=[dict(id="D1", xyz_m=[1, 2, 3])])
    verify_authored_route(world, route)
    world["points"][0]["xyz_m"][0] = 999
    world["points"][0]["world_xyz_m"][0] = 999
    with pytest.raises(ValueError, match="authored_source_waypoint_changed"):
        verify_authored_route(world, route)


@pytest.mark.parametrize("fault", ["settled_before_spawn", "stale_payload", "already_armed"])
def test_observed_cargo_mount_blocks_arming_when_fixture_did_not_hold_position(fault):
    from src.runtime.yokohama_payload import require_delivery_mount
    row = dict(sim_s=1, arming_state=1, landed=True,
               vehicle=dict(xyz=[0, 0, 0.3], age_s=0, sensor_sim_s=1, id=1),
               payload=dict(xyz=[0, 0, 0.16], age_s=0, sensor_sim_s=1, id=2))
    require_delivery_mount(config(), row, before_takeoff=True)
    if fault == "settled_before_spawn":
        row["payload"]["xyz"][2] = 0.04
    elif fault == "stale_payload":
        row["payload"]["age_s"] = 0.501
    else:
        row["arming_state"] = 2
    with pytest.raises(ValueError, match="cargo mount"):
        require_delivery_mount(config(), row, before_takeoff=True)
