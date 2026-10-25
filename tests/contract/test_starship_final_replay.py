"""Invented saved display records only, never evidence of flight/model success."""

from copy import deepcopy
from hashlib import sha256
import socket
import subprocess

import pytest

from src.runtime import starship_final_replay as replay
from src.runtime.starship_restored_replay import _wire, _digest


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("Pure replay must not render, run a process or contact a provider")

    for owner, name in ((socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def state(t, *, fuel=80000.0, engines=6):
    return {
        "time_s": float(t),
        "r_eci_m": [6378137.0 + 100000.0, float(t) * 100.0, 0.0],
        "v_eci_mps": [0.0, 100.0, 0.0],
        "q_body_to_eci": [1.0, 0.0, 0.0, 0.0],
        "omega_body_rad_s": [0.0, 0.0, 0.0],
        "propellant_kg": fuel,
        "engine_states": [
            {"throttle": 0.0, "gimbal_x_rad": 0.0, "gimbal_y_rad": 0.0, "available": True}
            for _ in range(engines)
        ],
        "flap_angles_rad": [0.0, 0.0, 0.0, 0.0],
    }


def sample(t, phase="orbital_coast", body="ship", *, engines=6, fuel=80000.0):
    return {
        **state(t, fuel=fuel, engines=engines),
        "phase": phase,
        "body_id": body,
        "altitude_m": 100000.0,
        "ground_speed_mps": 100.0,
        "com_z_m": 24.0,
        "mass_kg": 180000.0,
        "applied_thrust_n": 0.0,
        "engine_count": 0,
        "throttle": 0.0,
        "aero_panel_names": ["flap_aft_-1", "flap_aft_1", "flap_forward_-1", "flap_forward_1"],
        "main_engine_anchors": [[0.0, 0.0, 1.0] for _ in range(engines)],
        "main_engine_thrust_n": [0.0] * engines,
    }


def judged(action, mode="fixture", provider="typesafe", *, refs=None):
    live = mode == "live"
    return {
        "action": action,
        "status": "valid" if live else "fixture_valid",
        "mode": mode,
        "provider": provider,
        "model_inference_invoked": live,
        "summary": "Use the independently supplied observations.",
        "references": [] if refs is None else refs,
        "provider_receipt": {
            "model_id": "deepseek-flash" if provider == "deepseek" else "jev-1.13.0",
            "request_model": "deepseek-flash" if provider == "deepseek" else "jev-1.13.0",
            "status": "succeeded" if live else "fixture_succeeded",
            "mode": mode,
            "model_inference_invoked": live,
            "complete_response_observed": live,
            "call_succeeded": live,
            "native_choice_probabilities": {action: 1.0},
            "native_choice_confidence": 1.0,
        },
    }


@pytest.fixture
def case():
    manifest = {
        "src/runtime/starship_final_replay.py": "1" * 64,
        "examples/spaceflight/starship-sixdof-profile.json": "2" * 64,
    }
    context = {"run_id": "invented-replay-only", "mission_id": "synthetic", "vehicle_id": "ship"}
    execution = {
        "schema": "missionos.starship_final_execution.v2",
        "context": context,
        "mode": "fixture",
        "case_id": "T",
        "plan_sha256": "a" * 64,
        "source_sha256": manifest,
    }
    prefix_samples = [sample(0.0, "stack_ascent"), sample(1.0, "ship_ascent"), sample(2.0)]
    booster_samples = [
        sample(1.0, "booster_boostback_slew", "booster", engines=33, fuel=260000.0),
        sample(3.0, "booster_surface_contact", "booster", engines=33, fuel=50000.0),
    ]
    booster_samples[-1]["contact"] = True
    children = []
    for index in range(13):
        identity = "sat-" + str(index)
        points = [
            sample(1.5, "satellite_coast", identity, engines=0, fuel=0.0),
            sample(2.0, "satellite_coast", identity, engines=0, fuel=0.0),
        ]
        children.append(
            {
                "id": identity,
                "receipt": {"time_s": 1.5, "generation": index + 1},
                "release_state": state(1.5, engines=0, fuel=0.0),
                "samples": points,
                "orbital_propagation_only": True,
            }
        )
    inventory = {
        "released_count": 13,
        "retained_count": 13,
        "release_receipts": [x["receipt"] for x in children],
    }
    handoff = {
        "schema": "missionos.starship_final_actual_dispatch_handoff.v1",
        "coast_history": [{"state": state(t), "retained_count": 13} for t in (2.0, 3.0, 4.0)],
        "snapshot": {
            "state": state(4.0),
            "phase": "orbital_coast",
            "vehicle": {"unreviewed_snapshot": "not renderer input"},
        },
        "inventory": inventory,
        "completed_flight": False,
    }
    rollout_samples = [
        sample(4.0, "deorbit_burn"),
        sample(5.0, "terminal_burn"),
        sample(6.0, "surface_contact"),
    ]
    rollout_samples[-1]["contact"] = True
    mission = {
        "schema": replay.MISSION,
        "execution": execution,
        "prefix": {
            "samples": prefix_samples,
            "final_state": state(2.0),
            "events": [],
            "restored_prefix": {"closed": True},
            "booster_separation_state": state(1.0, fuel=260000.0, engines=33),
            "booster_run": {
                "samples": booster_samples,
                "events": [],
                "final_state": state(3.0, fuel=50000.0, engines=33),
                "booster_separation_state": state(1.0, fuel=260000.0, engines=33),
            },
            "satellites": children,
        },
        "handoff": handoff,
        "governance": {
            "schema": "missionos.starship_final_pre_dispatch_governance.v1",
            "completed_v1_flight": False,
            "deployment": {
                "decisions": [
                    {
                        "applied_at_actual_time_s": 1.6,
                        "applied_action": "withhold_deployment",
                        "fallback_used": False,
                    }
                ]
            },
            "decisions": [
                {
                    "actual_snapshot": {"state": state(3.0)},
                    "original_action": "select_next_orbit",
                    "disposition": "admitted",
                }
            ],
            "current_origin_changes": [
                {
                    "actual_snapshot": {"state": state(3.0)},
                    "confirmation_snapshot": {"state": state(3.5)},
                }
            ],
        },
        "fault_harness": {
            "selection": {"selected_family": "return_propulsion_80"},
            "injection": {"injection_time_s": 4.0},
        },
        "sensor_return": {
            "initial_state": state(4.0),
            "handoff_record": deepcopy(handoff),
            "handoff_sha256": _digest(handoff),
            "inventory": deepcopy(inventory),
            "profile": {
                "geometry": {"ship_length_m": 50.0},
                "launch": {"latitude_deg": 26.0, "longitude_deg": -97.0},
            },
            "rollout": {
                "initial_state": state(4.0),
                "final_state": state(6.0),
                "samples": rollout_samples,
                "events": [],
            },
            "fallback_ledger": [],
        },
        "supplemental": {
            "supervision": {"requests": [], "judgments": []},
            "visual": {"requests": [], "results": [], "events": []},
        },
    }
    hosts = {
        name: {
            "schema": replay.HOST,
            "channel": name,
            "plan_sha256": "a" * 64,
            "source_sha256": _digest(manifest),
            "context": context,
            "mode": "fixture",
            "record": {},
        }
        for name in replay.HOSTS
    }
    request = {"request_id": "return-choice", "time_s": 2.5, "deadline_s": 3.5, "phase": "return"}
    hosts["restored"]["record"] = {
        "decision_host": {
            "requests": [
                {
                    "frame": {"request": request},
                    "attempts": [
                        {
                            "provider": "deepseek",
                            "judgment": judged(
                                "request_scope_amendment",
                                provider="deepseek",
                                refs=[
                                    {
                                        "reference_id": "candidate.next_orbit.return_time_s",
                                        "value": 4.0,
                                    }
                                ],
                            ),
                        }
                    ],
                }
            ]
        },
        "operator_ledger": [
            {
                "request": {"issued_simulation_time_s": 3.0},
                "receipt": {"simulation_time_s": 3.0, "approved": True, "choice": "approve"},
                "consumption": {"simulation_time_s": 3.1},
            }
        ],
    }
    hosts["sensor"]["record"] = {"sessions": []}
    verified = {
        "schema": replay.VERIFICATION,
        "context": context,
        "plan_sha256": "a" * 64,
        "source_sha256": _digest(manifest),
        "mode": "fixture",
        "case_id": "T",
        "passed": True,
        "issues": [],
        "outcome": {
            "ship_contact_limits_met": True,
            "booster_contact_envelope_met": True,
            "all_required_physical_gates_met": True,
            "contact": {"time_s": 6.0, "speed_mps": 3.48},
        },
        "work": {
            "native_role_work_observed": {key: False for key in replay.NATIVE_ROLES},
            "all_native_role_work_observed": False,
            "scripted_scope_amendment_consistent": True,
            "sensor_tracking_requested": True,
            "sensor_once_only_application": True,
            "sensor_fallback_count": 0,
        },
    }
    return mission, hosts, verified, manifest


def build(case, **kwargs):
    mission, hosts, verified, manifest = case
    raw = _wire(mission)
    verified = deepcopy(verified)
    verified["record_sha256"] = sha256(raw).hexdigest()
    verify_raw = _wire(verified)
    host_raw = {key: _wire(value) for key, value in hosts.items()}
    arguments = {
        "expected_mission_sha256": sha256(raw).hexdigest(),
        "expected_verification_sha256": sha256(verify_raw).hexdigest(),
        "expected_plan_sha256": "a" * 64,
        "expected_source_sha256": manifest,
        "host_records": host_raw,
        "expected_host_sha256": {key: sha256(value).hexdigest() for key, value in host_raw.items()},
    }
    arguments.update(kwargs)
    return replay.build_final_replay(raw, verify_raw, **arguments)


def resolve(value, pointer):
    for name in pointer.split("/")[1:]:
        value = (
            value[int(name)]
            if type(value) is list
            else value[name.replace("~1", "/").replace("~0", "~")]
        )
    return value


def test_one_pinned_v2_flight_maps_saved_segments_no_legacy_completion(case):
    before = _wire(case)
    result = build(case)
    assert _wire(case) == before
    assert result["schema"] == replay.SCHEMA and result["playback_available"] is True
    ship = result["bodies"][0]
    assert ship["start_time_s"] == 0.0 and ship["end_time_s"] == 6.0
    assert ship["sample_count"] == 9 and ship["saved_points_removed"] == 0
    assert len(result["bodies"]) == 15 and len(result["release_receipts"]) == 13
    assert all(x["time_s"] == 1.5 for x in result["release_receipts"])
    assert result["role_coverage"]["missing_native_roles"] == sorted(replay.NATIVE_ROLES)
    assert not result["role_coverage"]["all_requested_native_roles_observed"]
    assert result["renderer_contract"]["attitude_source"] == "integrated_quaternion"
    assert result["renderer_contract"]["attitude_inferred_from_thrust_or_velocity"] is False
    assert result["display_policy"]["legacy_completed_v1_flight_fabricated"] is False
    assert not result["display_policy"]["render_or_video_produced"]
    assert all(b["end_time_s"] == 2.0 for b in result["bodies"][2:])


def test_v3_storage_preserves_the_same_recorded_replay(case):
    previous = build(case)
    case[0]["schema"] = "missionos.starship_final_native_mission.v3"
    case[2]["schema"] = "missionos.starship_final_native_verification.v3"
    current = build(case)
    for key in ("bodies", "release_receipts", "timeline", "judgments", "role_coverage"):
        assert current[key] == previous[key]


@pytest.mark.parametrize("changed", [0, 2])
def test_storage_and_saved_verification_versions_cannot_be_mixed(case, changed):
    case[changed]["schema"] = (
        "missionos.starship_final_native_mission.v3" if changed == 0
        else "missionos.starship_final_native_verification.v3"
    )
    with pytest.raises(ValueError, match="final_replay_pinned_matching_final_schema_required"):
        build(case)


def test_every_point_and_display_event_has_a_resolvable_raw_json_pointer(case):
    result = build(case)
    mission, hosts, verified, _ = case
    records = {
        "mission": mission,
        "verification": verified,
        **{"host." + name: value for name, value in hosts.items()},
    }
    for body in result["bodies"]:
        for segment in body["segments"]:
            for point in segment["samples"]:
                original = resolve(records[point["source"]["record"]], point["source"]["pointer"])
                for key in replay._state(original):
                    assert point[key] == original[key]
    for row in [*result["timeline"], *result["judgments"], *result["release_receipts"]]:
        assert resolve(records[row["source"]["record"]], row["source"]["pointer"]) is not None
    coast = [
        p
        for s in result["bodies"][0]["segments"]
        for p in s["samples"]
        if p["source"]["pointer"].startswith("/handoff/")
    ]
    assert all(
        "applied_thrust_n" in p["unavailable_display_fields"] and "applied_thrust_n" not in p
        for p in coast
    )


def test_scripted_approval_and_fixture_reasoning_never_become_human_or_native(case):
    result = build(case)
    assert result["judgments"][0]["classification"] == "fixed_response"
    assert (
        result["judgments"][0]["external_summary"] == "Use the independently supplied observations."
    )
    assert result["judgments"][0]["references"][0]["value"] == 4.0
    operator = [
        row for row in result["timeline"] if row["event"] == "scripted_scope_choice_received"
    ][0]
    assert operator["kind"] == "authenticated_scripted_test_principal_not_real_human_review"
    assert result["role_coverage"]["real_human_review"] is False
    assert result["public_data_fidelity"]["current_public_data_fit_reverified"] is False
    assert result["public_data_fidelity"]["mandatory_limits"]


@pytest.mark.parametrize("failure", ["verification", "physical"])
def test_failed_consistency_or_physics_is_retained_as_no_video_summary(case, failure):
    case[2]["passed" if failure == "verification" else "outcome"] = (
        False if failure == "verification" else {"all_required_physical_gates_met": False}
    )
    result = build(case)
    assert not result["playback_available"] and result["status"] == "retained_negative_no_video"
    assert result["bodies"] == result["timeline"] == result["judgments"] == []
    assert not result["display_policy"]["render_or_video_produced"]


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "plan",
        "manifest",
        "context",
        "mode",
        "verified_case",
        "host_plan",
        "host_source",
        "host_mode",
        "host_context",
    ],
)
def test_other_flight_identity_or_forged_legacy_v1_cannot_supply_current_replay(case, change):
    mission, hosts, verified, _ = case
    if change == "legacy":
        mission["schema"] = "missionos.starship_restored_native_mission.v1"
    elif change == "plan":
        mission["execution"]["plan_sha256"] = "b" * 64
    elif change == "manifest":
        mission["execution"]["source_sha256"] = {"src/old.py": "b" * 64}
    elif change == "context":
        mission["execution"]["context"] = {
            **mission["execution"]["context"],
            "run_id": "old-flight",
        }
    elif change == "mode":
        mission["execution"]["mode"] = "live"
    elif change == "verified_case":
        verified["case_id"] = "A"
    elif change == "host_plan":
        hosts["sensor"]["plan_sha256"] = "b" * 64
    elif change == "host_source":
        hosts["sensor"]["source_sha256"] = "b" * 64
    elif change == "host_mode":
        hosts["sensor"]["mode"] = "live"
    else:
        hosts["sensor"]["context"] = {**hosts["sensor"]["context"], "run_id": "old-flight"}
    with pytest.raises(ValueError):
        build(case)


@pytest.mark.parametrize(
    "change",
    [
        "prefix",
        "coast",
        "suffix",
        "rollout",
        "inventory",
        "booster",
        "satellite",
        "legacy_completion",
        "prefix_prediction",
    ],
)
def test_actual_seams_and_inventory_remain_checked_with_resealed_display_pins(case, change):
    mission = case[0]
    if change == "prefix":
        mission["prefix"]["samples"][-1]["propellant_kg"] -= 1.0
    elif change == "coast":
        mission["handoff"]["coast_history"][0]["state"]["propellant_kg"] -= 1.0
    elif change == "suffix":
        mission["sensor_return"]["initial_state"]["propellant_kg"] -= 1.0
    elif change == "rollout":
        mission["sensor_return"]["rollout"]["samples"][-1]["propellant_kg"] -= 1.0
    elif change == "inventory":
        mission["sensor_return"]["inventory"]["retained_count"] = 0
    elif change == "booster":
        mission["prefix"]["booster_run"]["booster_separation_state"]["propellant_kg"] -= 1.0
    elif change == "satellite":
        mission["prefix"]["satellites"][0]["release_state"]["propellant_kg"] = 1.0
    elif change == "legacy_completion":
        mission["governance"]["completed_v1_flight"] = True
    else:
        mission["prefix"]["restored_prefix"]["closed"] = False
    with pytest.raises(ValueError):
        build(case)


def test_same_clock_unexplained_state_change_is_not_smoothed_into_the_video(case):
    points = case[0]["sensor_return"]["rollout"]["samples"]
    wrong = deepcopy(points[0])
    wrong["propellant_kg"] -= 1.0
    points.insert(1, wrong)
    with pytest.raises(ValueError, match="unexplained_same_clock"):
        build(case)


def test_fixture_native_claim_and_external_fidelity_upgrade_are_rejected(case):
    case[2]["work"]["native_role_work_observed"]["deep_comparison"] = True
    with pytest.raises(ValueError, match="role_claim"):
        build(case)
    case[2]["work"]["native_role_work_observed"]["deep_comparison"] = False
    notes = {
        "source_manifest_sha256": _digest(case[3]),
        "real_vehicle_validated": True,
        "current_public_data_fit_reverified": True,
        "notes": [],
        "references": [],
    }
    with pytest.raises(ValueError, match="fidelity_disclosure"):
        build(case, public_data_fidelity=notes)


def test_internal_reasoning_is_never_exported_from_a_pinned_judgment(case):
    judgment = case[1]["restored"]["record"]["decision_host"]["requests"][0]["attempts"][0][
        "judgment"
    ]
    judgment["reasoning_content"] = "PRIVATE_CHAIN_OF_THOUGHT_SENTINEL"
    judgment["provider_receipt"]["raw_response"] = "PRIVATE_CHAIN_OF_THOUGHT_SENTINEL"
    result = build(case)
    assert b"PRIVATE_CHAIN_OF_THOUGHT_SENTINEL" not in _wire(result)
    assert all(row["internal_reasoning_recorded"] is False for row in result["judgments"])


def test_required_external_raw_pins_and_total_byte_budget(case):
    with pytest.raises(ValueError):
        build(case, expected_plan_sha256="wrong")
    with pytest.raises(ValueError):
        build(case, expected_mission_sha256="0" * 64)
    with pytest.raises(ValueError):
        build(case, expected_host_sha256={"sensor": "0" * 64})
    with pytest.raises(ValueError, match="total_input_budget"):
        build(case, maximum_input_bytes=100)


def test_supplemental_visual_supervision_sensor_and_native_labels_use_distinct_current_sources(
    case,
):
    mission, hosts, verified, _ = case
    ascent_request = {"request_id": "ascent-current", "time_s": 0.2, "deadline_s": 0.9}
    ascent = {
        **judged("continue_mission"),
        "schema": "missionos.starship_final_supervision_judgment.v1",
    }
    mission["supplemental"]["supervision"] = {
        "requests": [{"request_id": "ascent-current", "request": ascent_request}],
        "judgments": [
            {
                "request_id": "ascent-current",
                "domain": "ascent",
                "time_s": 0.3,
                "accepted": True,
                "action": "continue_mission",
                "result": ascent,
                "safety_path": None,
            }
        ],
    }
    visual_request = {"request_id": "visual-current", "snapshot_time_s": 1.6, "deadline_s": 2.1}
    visual = {
        **judged("request_numeric_confirmation"),
        "schema": "missionos.starship_visual_result.v1",
        "observation_choice": "fresh_separation_counter",
        "visual_diagnosis": {
            "visibility": "adequate",
            "telemetry_relation": "insufficient",
            "recommended_action": "request_numeric_confirmation",
        },
    }
    mission["supplemental"]["visual"] = {
        "requests": [{"envelope_sha256": "b" * 64, "request": visual_request}],
        "results": [
            {
                "envelope_sha256": "b" * 64,
                "time_s": 1.7,
                "accepted": True,
                "reply": {"result": visual},
            }
        ],
        "events": [],
    }
    sensor_request = {"request_id": "sensor-current", "issued_at_s": 4.0, "deadline_s": 5.0}
    sensor_call = judged(
        "adopt_accelerometer_estimate",
        provider="deepseek",
        refs=[{"reference_id": "accel_factor", "value": 0.8}],
    )
    hosts["sensor"]["record"]["sessions"] = [
        {"request": sensor_request, "assessments": [sensor_call]}
    ]
    result = build(case)
    assert [row["phase"] for row in result["judgments"]] == [
        "ascent",
        "visual_observation",
        "pre_dispatch_return",
        "return_sensor_judgment",
    ]
    assert result["judgment_counts"] == {
        "confirmed_native_judgment": 0,
        "fixed_response": 4,
        "unavailable_or_unaccepted": 0,
    }
    image = result["judgments"][1]
    assert (
        image["request_time_s"] == 1.6 and image["observation_choice"] == "fresh_separation_counter"
    )
    assert image["visual_findings"]["telemetry_relation"] == "insufficient"
    assert image["adopted_by_worker"] is True and image["proposal_is_approval_or_dispatch"] is False
    assert result["judgments"][-1]["source"]["record"] == "host.sensor"
    assert result["judgments"][-1]["references"] == [{"reference_id": "accel_factor", "value": 0.8}]


def test_invented_live_shaped_record_reports_only_observed_roles_without_filling_the_rest(case):
    mission, hosts, verified, _ = case
    mission["execution"]["mode"] = verified["mode"] = "live"
    for host in hosts.values():
        host["mode"] = "live"
    hosts["restored"]["record"]["decision_host"]["requests"][0]["attempts"][0]["judgment"] = judged(
        "request_scope_amendment", mode="live", provider="deepseek"
    )
    verified["work"]["native_role_work_observed"]["return_selection"] = True
    verified["work"]["native_role_work_observed"]["deep_comparison"] = True
    result = build(case)
    assert result["judgments"][0]["classification"] == "confirmed_native_judgment"
    assert result["judgment_counts"]["confirmed_native_judgment"] == 1
    assert len(result["role_coverage"]["missing_native_roles"]) == 5
    assert not result["role_coverage"]["all_requested_native_roles_observed"]
    assert not result["role_coverage"]["native_role_observed_is_judgment_quality_score"]


def test_unavailable_providers_and_rules_fallback_remain_visible_without_model_success(case):
    mission, hosts, verified, _ = case
    hosts["restored"]["record"]["decision_host"]["requests"][0]["attempts"][0]["judgment"] = None
    mission["supplemental"]["supervision"] = {
        "requests": [
            {
                "request_id": "expired-ascent",
                "request": {"request_id": "expired-ascent", "time_s": 0.2, "deadline_s": 0.9},
            }
        ],
        "judgments": [
            {
                "request_id": "expired-ascent",
                "domain": "ascent",
                "time_s": 1.0,
                "accepted": False,
                "action": None,
                "result": None,
                "safety_path": "withhold_future_deployment",
            }
        ],
    }
    mission["sensor_return"]["fallback_ledger"] = [
        {"time_s": 5.0, "action": "continue_nominal_assumption"}
    ]
    verified["work"]["sensor_fallback_count"] = 1
    result = build(case)
    assert (
        len(result["judgments"]) == 2
        and result["judgment_counts"]["unavailable_or_unaccepted"] == 2
    )
    assert result["judgments"][0]["safety_path"] == "withhold_future_deployment"
    assert result["judgments"][0]["observed_unavailable_time_s"] == 1.0
    assert result["role_coverage"]["sensor_fallback_count"] == 1
    assert any(row["kind"] == "preapproved_fallback_not_ai_judgment" for row in result["timeline"])
    assert any(
        row["kind"] == "preapproved_safety_path_not_ai_judgment" for row in result["timeline"]
    )


def test_independent_tracking_delivery_and_return_review_routing_are_displayed_as_saved_facts(case):
    case[0]["sensor_return"]["tracking_delivery"] = {"requested_at_s": 4.2, "received_at_s": 4.7}
    case[1]["restored"]["record"]["decision_host"]["requests"][0]["delivered_result"] = {
        "return_review": {"route": "rules_uncertainty_review", "review_required": True}
    }
    result = build(case)
    assert result["judgments"][0]["return_review_route"]["rules_review_is_jev_delegation"] is False
    tracking = [
        row for row in result["timeline"] if row["event"].startswith("independent_tracking_")
    ]
    assert [row["time_s"] for row in tracking] == [4.2, 4.7]
    assert all(row["kind"] == "recorded_independent_observation_tool_fact" for row in tracking)


@pytest.mark.parametrize("key", ["ship_contact_limits_met", "booster_contact_envelope_met"])
def test_one_failed_physical_gate_cannot_be_hidden_by_an_aggregate_true_flag(case, key):
    case[2]["outcome"][key] = False
    assert build(case)["playback_available"] is False


def test_verified_contact_endpoint_cannot_be_retimed_for_presentation(case):
    case[2]["outcome"]["contact"]["time_s"] = 5.0
    with pytest.raises(ValueError, match="contact_endpoint"):
        build(case)
