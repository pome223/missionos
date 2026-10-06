"""Independent receipt arithmetic. Mocked forecasts are not physical execution."""

from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_actual_recovery_planning_verifier as checker
from src.runtime import starship_actual_recovery_shooting as producer
from src.runtime import starship_constrained_recovery_verifier as recovery
from src.runtime.starship_booster_recovery_verifier import _Invalid


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(scope="module")
def setup():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
    from src.runtime.starship_booster_catch import configuration

    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(
        json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    )
    body = vehicle(profile, "booster")
    position = env.surface_state(
        profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 50000.0, time_s=100.0
    )
    up, east, _ = env.local_frame(position)
    q = tuple(float(x) for x in _attitude(up, east))
    state = dyn.State6DOF(
        100.0,
        position.r,
        env.add(position.v, env.scale(up, -50.0)),
        q,
        (0.0, 0.0, 0.0),
        78000.0,
        tuple(dyn.EngineState() for _ in body.engines),
        tuple(0.0 for _ in body.aero_panels),
    )
    frame = ConditionedGeographicFrame(q, 100.0, maximum_roll_rate_rad_s=0.03 / 0.28)
    seed = {
        "burn_axis_enu": [0.0, 0.0, 1.0],
        "target_velocity_enu_mps": [0.0, 0.0, -50.0],
        "bank_heading_enu": [1.0, 0.0, 0.0],
        "bank_sign": -1,
        "prediction_is_execution": False,
    }
    capsule = producer.capture_context(
        state,
        profile,
        catch,
        recovery._CONFIGURATIONS[recovery._TRACKING_POLICY],
        phase="recovery_boostback_slew",
        start_time_s=100.0,
        deadline_s=1300.0,
        plan=seed,
        refreshed=True,
        burn_start_s=None,
        settle_start_s=None,
        previous_entry_axis_enu=None,
        entry_pretrim_prepared_at_s=None,
        next_preview_s=100.0,
        braking_preview=None,
        prior_command_reference=None,
        conditioned_reference=frame,
        reference_tracker=None,
    )
    return profile, catch, body, state, capsule


def synthetic_forecast(setup, parameters=(0.0, 0.0, 0.0)):
    # Known initialized geometry, not a trajectory integration or arrival proof.
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude
    from src.runtime.starship_booster_recovery import _tower_observation

    profile, catch, body, origin, _ = setup
    fuel = 78000.0
    center = dyn.mass_properties(body, fuel).com_body_m[2]
    altitude = catch["support_height_m"] - catch["support_points_body_m"][0][2] + center + 3.4
    location = env.surface_state(
        profile["launch"]["latitude_deg"],
        profile["launch"]["longitude_deg"],
        altitude,
        time_s=100.1,
    )
    up, east, north = env.local_frame(location)
    r = env.add(
        location.r,
        env.add(
            env.scale(east, 0.03 + 200 * parameters[0] + 0.01 * parameters[2]),
            env.scale(north, -0.01 + 200 * parameters[1]),
        ),
    )
    q = tuple(float(x) for x in _attitude(up, east))
    relative = env.add(
        env.scale(up, -1.75 + 0.02 * parameters[2]),
        env.add(
            env.scale(east, 0.1 + 20 * parameters[0]), env.scale(north, 0.03 + 20 * parameters[1])
        ),
    )
    state = replace(
        origin,
        time_s=100.1,
        r_eci_m=r,
        v_eci_mps=env.add(location.v, relative),
        q_body_to_eci=q,
        omega_body_rad_s=dyn.inverse_rotate(q, (0.0, 0.0, env.EARTH_ROTATION_RAD_S)),
    )
    command = dyn.Command6DOF(
        tuple(dyn.EngineCommand(i < 3, 0.4 if i < 3 else 0.0) for i in range(len(body.engines))),
        state.flap_angles_rad,
    )
    observation = _tower_observation(state, body, profile, catch)
    first = {
        "time_s": 100.0,
        "phase": "recovery_boostback_slew",
        "state": asdict(origin),
        "command": asdict(command),
        "com_rate_body_mps": [0.0, 0.0, 0.0],
        "navigation": {},
    }
    point = {
        "time_s": 100.1,
        "phase": "recovery_landing_13",
        "state": asdict(state),
        "command": asdict(command),
        "com_rate_body_mps": [0.0, 0.0, 0.0],
        "navigation": {"arrival_observation": observation},
    }
    terminal_state = replace(state, time_s=100.2)
    terminal = {
        "time_s": 100.2,
        "phase": "recovery_landing_13",
        "state": asdict(terminal_state),
        "command": None,
        "com_rate_body_mps": [0.0, 0.0, 0.0],
        "navigation": {},
    }
    return saved(
        {
            "guidance_policy": "constrained_return_development_v8",
            "final_state": asdict(terminal_state),
            "recovery_record": {
                "checkpoints": [first, point, terminal],
                "handoff": {
                    "eligible": False,
                    "observation": _tower_observation(terminal_state, body, profile, catch),
                },
            },
            "outcome": {"integration_steps": 2, "termination": "surface_contact"},
            "contact": None,
            "forecast_metadata": {"prediction_complete": True, "failure": None},
        }
    )


def make(
    monkeypatch,
    setup,
    *,
    missing=False,
    failure=None,
    partial=None,
    eligible_high_cost=False,
    transported=False,
):
    profile, catch, _, _, capsule = setup
    if transported:
        capsule = deepcopy(capsule)
        physical = recovery._CONFIGURATIONS[recovery._TRANSPORT_POLICY]
        capsule.update(
            schema=checker.TRANSPORT_CONTEXT_SCHEMA,
            physical_guidance_configuration=deepcopy(physical),
            guidance_configuration_sha256=checker._digest(physical),
        )
        capsule["context"]["prepare_tail_start_s"] = None
    count = 0
    records = {}

    def fake(snapshot, p, c, plan, request):
        nonlocal count
        count += 1
        if count == failure:
            raise RuntimeError("fixture failure")
        axis = plan["burn_axis_enu"]
        parameters = [
            math.atan2(axis[0], axis[2]),
            math.atan2(axis[1], axis[2]),
            sum(
                (a - b) * x
                for a, b, x in zip(
                    plan["target_velocity_enu_mps"],
                    capsule["context"]["plan"]["target_velocity_enu_mps"],
                    axis,
                )
            ),
        ]
        run = synthetic_forecast(setup, parameters)
        if eligible_high_cost and count == 2:
            from src.runtime import starship_physics as env, starship_sixdof as dyn
            from src.runtime.starship_booster_recovery import _tower_observation

            # Static terminal geometry whose handoff gate holds but whose L2
            # score is deliberately worse than the baseline's earlier point.
            run = synthetic_forecast(setup)
            terminal = deepcopy(run["recovery_record"]["checkpoints"][1])
            state = dyn.state_from_dict(terminal["state"])
            _, east, _ = env.local_frame(
                env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
            )
            state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(east, 0.14)))
            observation = _tower_observation(state, setup[2], setup[0], setup[1])
            assert observation["eligible"]
            terminal.update(state=saved(asdict(state)), command=None, navigation={})
            run["recovery_record"].update(
                checkpoints=[run["recovery_record"]["checkpoints"][0], terminal],
                handoff={"eligible": True, "observation": saved(observation)},
            )
            run["final_state"] = terminal["state"]
            run["outcome"].update(integration_steps=1, termination="catch_handoff")
        if count == partial:
            run["forecast_metadata"]["prediction_complete"] = False
            run["outcome"]["termination"] = "forecast_wall_limit"
        return run

    def sink(identifier, payload):
        records[identifier] = deepcopy(payload)
        digest = producer.digest(payload)
        return {
            "artifact_id": identifier,
            "format": "json",
            "relative_path": identifier + ".json",
            "sha256": digest,
            "raw_json_sha256": digest,
            "bytes": len(json.dumps(payload)),
            "persisted_before_analysis": True,
        }

    monkeypatch.setattr(producer, "forecast_constrained_continuation", fake)
    reference = None if missing else synthetic_forecast(setup)
    arguments = {}
    if transported:
        reference["guidance_policy"] = recovery._TRANSPORT_POLICY
        reference["recovery_record"].update(
            guidance_configuration=deepcopy(physical),
            input_separation_state=deepcopy(capsule["state"]),
        )
        reference["booster_separation_state"] = deepcopy(capsule["state"])
        arguments["baseline_binding"] = {
            "run_sha256": producer.digest(reference),
            "profile_sha256": producer.digest(profile),
            "catch_profile_sha256": producer.digest(catch),
        }
    plan, receipt = producer.refine_actual_recovery_plan(
        capsule,
        profile,
        catch,
        capsule["context"]["plan"],
        artifact_sink=sink,
        baseline_reference=reference,
        **arguments,
    )
    assert count == receipt["attempted_forecast_count"]
    assert len(records) == 1 + 2 * count
    return plan, records


def verify(plan, setup):
    policy = (
        recovery._TRANSPORT_POLICY
        if plan["actual_recovery_prediction"]["schema"] == checker.TRANSPORT_SCHEMA
        else recovery._TRACKING_POLICY
    )
    return checker.verify_actual_recovery_prediction(
        plan,
        setup[4]["state"],
        setup[0],
        setup[1],
        recovery._CONFIGURATIONS[policy],
    )


def test_producer_and_independent_contracts_match():
    assert checker.CONFIG == producer.CONFIG
    assert recovery._CONFIGURATIONS[recovery._ACTUAL_POLICY]["actual_planning"] == producer.CONFIG


def test_scored_8_call_fixture_binds_context_parameters_and_same_time_objective(monkeypatch, setup):
    plan, records = make(monkeypatch, setup)
    item = verify(plan, setup)
    assert item["attempted_forecast_count"] == 8
    assert item["baseline_equivalence"]["matched"]
    assert all(
        item[key] is False
        for key in (
            "production_policy_admitted",
            "arrival_admitted",
            "support_admitted",
            "physical_execution",
        )
    )
    assert len(records) == 17


@pytest.mark.parametrize("failure,partial", ((3, None), (None, 3)))
def test_failed_and_partial_calls_remain_persisted_and_unselectable(
    monkeypatch, setup, failure, partial
):
    plan, _ = make(monkeypatch, setup, failure=failure, partial=partial)
    item = verify(plan, setup)
    assert item["attempted_forecast_count"] == 7
    assert item["failed_forecast_count"] == int(failure is not None)
    assert item["partial_forecast_count"] == int(partial is not None)
    assert item["forecasts"][2]["score"] is None
    assert item["selected_attempt_index"] != 3


def test_missing_external_baseline_stops_after_one_persisted_call(monkeypatch, setup):
    plan, _ = make(monkeypatch, setup, missing=True)
    item = verify(plan, setup)
    assert item["attempted_forecast_count"] == 1 and item["selected_attempt_index"] == 1
    assert item["optimizer_status"] == "baseline_equivalence_not_established"
    assert item["selected_parameters"] == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    "mutation",
    (
        "context_state",
        "context_clock",
        "context_profile",
        "context_frame",
        "seed",
        "count",
        "partial_count",
        "parameters",
        "candidate_vector",
        "candidate_hash",
        "candidate_context",
        "request_context",
        "request_duration",
        "request_steps",
        "deadline_reset",
        "score_clock",
        "score_state",
        "score_vector",
        "score_objective",
        "score_source",
        "missing_raw",
        "path_escape",
        "id_reuse",
        "persistence",
        "baseline_count",
        "baseline_hash",
        "selected",
        "joint",
        "authority",
    ),
)
def test_material_receipt_mutations_fail_closed(monkeypatch, setup, mutation):
    plan, _ = make(monkeypatch, setup)
    item = plan["actual_recovery_prediction"]
    entry = item["forecasts"][1]
    score = entry["score"]
    if mutation == "context_state":
        item["origin_context"]["state"]["propellant_kg"] += 1
    elif mutation == "context_clock":
        item["origin_context"]["context"]["deadline_s"] += 1
    elif mutation == "context_profile":
        item["origin_context"]["profile_sha256"] = "a" * 64
    elif mutation == "context_frame":
        item["origin_context"]["context"]["conditioned_reference"]["time_s"] += 1
    elif mutation == "seed":
        item["origin_context"]["context"]["plan"]["bank_sign"] = 1
    elif mutation == "count":
        item["attempted_forecast_count"] -= 1
    elif mutation == "partial_count":
        item["partial_forecast_count"] += 1
    elif mutation == "parameters":
        entry["parameters"][0] += 0.002
    elif mutation == "candidate_vector":
        entry["candidate_request"]["burn_axis_enu"][0] += 0.01
    elif mutation == "candidate_hash":
        entry["candidate_plan_sha256"] = "a" * 64
    elif mutation == "candidate_context":
        entry["candidate_context_sha256"] = "a" * 64
    elif mutation == "request_context":
        entry["request"]["origin_context_sha256"] = item["origin_context_sha256"]
    elif mutation == "request_duration":
        entry["request"]["duration_s"] = 601.0
    elif mutation == "request_steps":
        entry["request"]["maximum_integration_steps"] = 6501
    elif mutation == "deadline_reset":
        entry["request"]["wall_deadline_monotonic_s"] += 1
    elif mutation == "score_clock":
        score["time_s"] += 0.1
    elif mutation == "score_state":
        score["checkpoint"]["state"]["propellant_kg"] += 1
    elif mutation == "score_vector":
        score["residual"][0] += 0.01
    elif mutation == "score_objective":
        score["objective"] += 1
    elif mutation == "score_source":
        score["observation_source"] = "ideal_point_coast"
    elif mutation == "missing_raw":
        del entry["raw_artifact"]
    elif mutation == "path_escape":
        entry["raw_artifact"]["relative_path"] = "../" + entry["raw_artifact"]["relative_path"]
    elif mutation == "id_reuse":
        entry["raw_artifact"] = deepcopy(entry["attempted_artifact"])
    elif mutation == "persistence":
        entry["raw_artifact"]["persisted_before_analysis"] = False
    elif mutation == "baseline_count":
        item["baseline_equivalence"]["actual_checkpoint_count"] += 1
    elif mutation == "baseline_hash":
        item["baseline_equivalence"]["expected_checkpoint_sha256"] = "a" * 64
    elif mutation == "selected":
        item["selected_attempt_index"] = 2 if item["selected_attempt_index"] != 2 else 1
    elif mutation == "joint":
        item["joint_construction"]["bounded_scaled_step"][0] += 0.1
    else:
        item["arrival_admitted"] = True
    with pytest.raises(_Invalid):
        verify(plan, setup)


@pytest.mark.parametrize("rank", (0, 1, 2, 3))
def test_stdlib_minimum_norm_joint_solve_matches_numpy_on_rank_deficiency(rank):
    rng = np.random.default_rng(17)
    matrix = rng.normal(size=(14, rank)) @ rng.normal(size=(rank, 3)) if rank else np.zeros((14, 3))
    rhs = rng.normal(size=14)
    expected = np.linalg.lstsq(matrix, rhs, rcond=1e-10)[0]
    assert checker._least_squares(matrix.tolist(), rhs.tolist()) == pytest.approx(
        expected, abs=1e-8
    )


def test_terminal_scoring_binds_final_observation_without_mutating_raw_checkpoint(setup):
    forecast = synthetic_forecast(setup)
    point = forecast["recovery_record"]["checkpoints"][-1]
    before = deepcopy(point)
    item = producer.checkpoint_score(
        point,
        observation=forecast["recovery_record"]["handoff"]["observation"],
        observation_source="terminal_handoff_observation",
    )
    item["checkpoint_index"] = 2
    entry = {"integration_steps": 2, "request": {"duration_s": 600.0}}
    checker._score(item, entry, setup[4]["state"], setup[0], setup[1])
    assert point == before and point["navigation"] == {}
    item["checkpoint_index"] = 1
    with pytest.raises(_Invalid):
        checker._score(item, entry, setup[4]["state"], setup[0], setup[1])


def test_checker_imports_neither_producer_dynamics_numpy_nor_scipy():
    text = inspect.getsource(checker)
    for forbidden in (
        "starship_actual_recovery_shooting import",
        "starship_sixdof import",
        "starship_constrained_recovery import",
        "import numpy",
        "import scipy",
    ):
        assert forbidden not in text


def test_v2_prioritizes_predicted_handoff_over_lower_l2_and_keeps_v1_ranking(monkeypatch, setup):
    plan, _ = make(monkeypatch, setup, eligible_high_cost=True)
    item = verify(plan, setup)
    assert item["forecasts"][1]["score"]["objective"] > item["forecasts"][0]["score"]["objective"]
    assert item["selected_attempt_index"] == 2
    old = deepcopy(plan)
    receipt = old["actual_recovery_prediction"]
    receipt.update(schema=checker.SCHEMA_V1, configuration=deepcopy(checker.CONFIG_V1))
    eligible = [
        entry
        for entry in receipt["forecasts"]
        if entry["status"] == "completed" and entry["score"] is not None
    ]
    chosen = min(eligible, key=lambda entry: (entry["score"]["objective"], entry["attempt_index"]))
    receipt.update(
        selected_attempt_index=chosen["attempt_index"], selected_parameters=chosen["parameters"]
    )
    old.update(chosen["candidate_request"])
    assert verify(old, setup)["selected_attempt_index"] != 2


def test_schema_and_configuration_cannot_cross_versions(monkeypatch, setup):
    plan, _ = make(monkeypatch, setup)
    plan["actual_recovery_prediction"]["schema"] = checker.SCHEMA_V1
    with pytest.raises(_Invalid):
        verify(plan, setup)


def test_equal_trace_digests_cannot_be_declared_baseline_mismatch(monkeypatch, setup):
    plan, _ = make(monkeypatch, setup)
    receipt = plan["actual_recovery_prediction"]
    receipt.update(schema=checker.SCHEMA_V1, configuration=deepcopy(checker.CONFIG_V1))
    receipt["baseline_equivalence"].update(matched=False, reason="baseline_continuation_mismatch")
    with pytest.raises(_Invalid):
        verify(plan, setup)


def test_schema3_uses_full_transport_context_and_matching_baseline_binding(monkeypatch, setup):
    plan, _ = make(monkeypatch, setup, transported=True)
    receipt = verify(plan, setup)
    assert receipt["schema"] == producer.TRANSPORT_SCHEMA == checker.TRANSPORT_SCHEMA
    assert receipt["configuration"] == producer.TRANSPORT_CONFIG == checker.TRANSPORT_CONFIG
    assert (
        recovery._CONFIGURATIONS[recovery._ACTUAL_TRANSPORT_POLICY]["actual_planning"]
        == producer.TRANSPORT_CONFIG
    )
    assert receipt["origin_context"]["schema"] == checker.TRANSPORT_CONTEXT_SCHEMA
    assert (
        receipt["reference_policy_id"]
        == receipt["expected_reference_policy_id"]
        == recovery._TRANSPORT_POLICY
    )
    assert receipt["baseline_binding_is_caller_assertion"] is True
    assert receipt["source_binding_independently_verified"] is False


@pytest.mark.parametrize(
    "mutation",
    (
        "old_reference",
        "profile",
        "catch",
        "run",
        "physical_law",
        "context_schema",
        "authenticated_source",
        "old_receipt_schema",
    ),
)
def test_schema3_cannot_mix_old_reference_inputs_or_claim_source_authentication(
    monkeypatch, setup, mutation
):
    plan, _ = make(monkeypatch, setup, transported=True)
    receipt = plan["actual_recovery_prediction"]
    if mutation == "old_reference":
        receipt["reference_policy_id"] = recovery._TRACKING_POLICY
    elif mutation in ("profile", "catch", "run"):
        receipt["baseline_binding"][
            {"profile": "profile_sha256", "catch": "catch_profile_sha256", "run": "run_sha256"}[
                mutation
            ]
        ] = "a" * 64
    elif mutation == "physical_law":
        receipt["origin_context"]["physical_guidance_configuration"]["powered_prepare_reference"][
            "shutdown_tail_tau_multiplier"
        ] = 3.0
    elif mutation == "context_schema":
        receipt["origin_context"]["schema"] = checker.CONTEXT_SCHEMA
    elif mutation == "authenticated_source":
        receipt["source_binding_independently_verified"] = True
    else:
        receipt.update(schema=checker.SCHEMA, configuration=deepcopy(checker.CONFIG))
    with pytest.raises(_Invalid):
        verify(plan, setup)


def test_transport_context_rejects_old_v8_baseline_before_any_forecast(monkeypatch, setup):
    profile, catch, _, _, original = setup
    capsule = deepcopy(original)
    physical = recovery._CONFIGURATIONS[recovery._TRANSPORT_POLICY]
    capsule.update(
        schema=checker.TRANSPORT_CONTEXT_SCHEMA,
        physical_guidance_configuration=deepcopy(physical),
        guidance_configuration_sha256=checker._digest(physical),
    )
    capsule["context"]["prepare_tail_start_s"] = None
    reference = synthetic_forecast(setup)
    binding = {
        "run_sha256": producer.digest(reference),
        "profile_sha256": producer.digest(profile),
        "catch_profile_sha256": producer.digest(catch),
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Mismatched old physical law reached a forecast or artifact write")

    monkeypatch.setattr(producer, "forecast_constrained_continuation", forbidden)
    with pytest.raises(ValueError, match="matching_v10_reference_inputs"):
        producer.refine_actual_recovery_plan(
            capsule,
            profile,
            catch,
            capsule["context"]["plan"],
            artifact_sink=forbidden,
            baseline_reference=reference,
            baseline_binding=binding,
        )
