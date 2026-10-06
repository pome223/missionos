"""Independent static-reference arithmetic, without trajectory integration."""

from copy import deepcopy
import json
import math
from pathlib import Path
import runpy
import time

import pytest

from src.runtime import starship_coupled_terminal_reference as producer
from src.runtime import starship_coupled_terminal_reference_verifier as check
from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime.starship_reference_tracking import ReferenceTracker
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime import starship_actual_recovery_shooting as shooting

PUBLIC = runpy.run_path(str(Path(__file__).with_name("test_starship_fixed_terminal_reference.py")))


@pytest.fixture
def query():
    profile, catch, body, initial, snapshot, prior, command = PUBLIC["fixture"]()
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    sample = reference.evaluate_terminal_reference(plan, initial.time_s + plan["duration_s"] / 2)
    request = {"q": list(initial.q_body_to_eci), "rate_eci_rad_s": [0.0, 0.01, 0.0],
               "acceleration_eci_rad_s2": [0.0, 0.001, 0.0], "fuel_kg": 40000.0,
               "reference_sample": sample}
    raw = producer._static_query(sample, request["q"], request["rate_eci_rad_s"],
        request["acceleration_eci_rad_s2"], request["fuel_kg"], initial, body, profile, catch)
    return json.loads(json.dumps((raw, request, plan, snapshot["state"], profile, catch)))


def test_calm_configured_load_and_required_force_arithmetic_no_actual_claim(query):
    original = deepcopy(query)
    result = check._load(*query)
    assert result == pytest.approx(query[0]["required_main_force_body_n"], abs=1e-4)
    assert query == original


@pytest.mark.parametrize("change", ["mass", "com", "gravity", "cg_acceleration", "panel_force",
    "panel_torque", "required_force", "source_sample", "actual_claim", "future_fin", "mass_convention", "extra"])
def test_stored_hypothetical_load_mutations_fail(query, change):
    data = list(deepcopy(query))
    raw = data[0]
    if change == "mass":
        raw["mass_kg"] += 1.0
    elif change == "com":
        raw["com_body_m"][2] += 0.01
    elif change == "gravity":
        raw["gravity_acceleration_eci_mps2"][0] += 0.01
    elif change == "cg_acceleration":
        raw["cg_reference_acceleration_eci_mps2"][0] += 0.01
    elif change == "panel_force":
        raw["panel_loads"][3]["force_body_n"][0] += 1.0
    elif change == "panel_torque":
        raw["panel_loads"][3]["torque_body_nm"][0] += 1.0
    elif change == "required_force":
        raw["required_main_force_body_n"][0] += 1.0
    elif change == "source_sample":
        raw["reference_sample"]["position_enu_m"][0] += 0.01
    elif change == "actual_claim":
        raw["integrated_observation"] = True
    elif change == "future_fin":
        raw["future_fin_angles_assumption"] = "known_future_angles"
    elif change == "mass_convention":
        raw["mass_kinematics_convention"] = "physical_actuator_spool_mass_flow"
    else:
        raw["extra"] = True
    with pytest.raises(ValueError):
        check._load(*data)


def tracking_fixture():
    profile, _, _, initial, _, _, _ = PUBLIC["fixture"]()
    tracker = ReferenceTracker(profile, initial.q_body_to_eci, initial.time_s - 0.1,
        initial_reference_rate_eci_rad_s=(0.0, 0.01, 0.0))
    previous = {"quaternion": list(tracker.quaternion), "time_s": tracker.time_s,
        "rate_eci_rad_s": list(tracker.rate_eci_rad_s), "raw_goal_q": list(tracker.raw_goal_q),
        "raw_goal_time_s": tracker.raw_goal_time_s, "first_update": tracker.first_update}
    goal = check._increment(list(initial.q_body_to_eci), 0.15, -0.1)
    _, tracking = tracker.update(goal, initial.q_body_to_eci, [0.0, 0.01, 0.0], initial.time_s)
    return tracking["receipt"], previous, goal, list(initial.q_body_to_eci), [0.0, 0.01, 0.0], initial.time_s, profile


def test_cloned_governor_carries_previous_rate_goal_and_pose_without_reset():
    data = tracking_fixture()
    result = check._tracking(*data)
    assert result["quaternion"] == pytest.approx(data[0]["requested_q_body_to_eci"])
    assert result["rate_eci_rad_s"] == pytest.approx(data[0]["reference_rate_eci_rad_s"])


@pytest.mark.parametrize("change", ["past_q", "past_rate", "past_goal", "clock", "rate_cap",
    "acceleration_cap", "requested_q", "rate", "acceleration", "actual_q", "actual_reset"])
def test_cloned_governor_changed_history_or_bounds_fail(change):
    data = list(tracking_fixture())
    item = data[0]
    field = {"past_q": "previous_requested_q_body_to_eci", "past_rate": "previous_reference_rate_eci_rad_s",
        "past_goal": "previous_raw_goal_q_body_to_eci", "requested_q": "requested_q_body_to_eci",
        "rate": "reference_rate_eci_rad_s", "acceleration": "reference_acceleration_eci_rad_s2",
        "actual_q": "actual_q_body_to_eci"}.get(change)
    if field:
        item[field][1] += 0.01
    elif change == "clock":
        item["previous_time_s"] -= 0.1
    elif change == "rate_cap":
        item["maximum_reference_rate_rad_s"] *= 2
    elif change == "acceleration_cap":
        item["maximum_reference_acceleration_rad_s2"] *= 2
    else:
        item["actual_state_assigned"] = True
    with pytest.raises(ValueError):
        check._tracking(*data)


@pytest.mark.parametrize("flag,field,limit", [
    ("goal_rate_clipped", "unbounded_goal_rate_eci_rad_s", 0.03 / 0.28),
    ("rate_clipped", "unbounded_reference_rate_eci_rad_s", 0.03 / 0.28),
    ("acceleration_clipped", "unbounded_reference_acceleration_eci_rad_s2", 0.03)])
@pytest.mark.parametrize("side", [-1, 0, 1])
def test_recorded_clipping_uses_exact_verified_input_boundary_and_source_norm(flag, field, limit, side):
    item = {"unbounded_goal_rate_eci_rad_s": [0.0, 0.0, 0.0],
        "unbounded_reference_rate_eci_rad_s": [0.0, 0.0, 0.0],
        "unbounded_reference_acceleration_eci_rad_s2": [0.0, 0.0, 0.0],
        "goal_rate_clipped": False, "rate_clipped": False, "acceleration_clipped": False}
    value = math.nextafter(limit, 0.0 if side < 0 else math.inf) if side else limit
    item[field] = [value, 0.0, 0.0]
    item[flag] = side > 0
    check._recorded_clipping(item, 0.03 / 0.28, 0.03)
    item[flag] = not item[flag]
    with pytest.raises(ValueError, match="governor_clipping_arithmetic"):
        check._recorded_clipping(item, 0.03 / 0.28, 0.03)


@pytest.mark.parametrize("flag", ["goal_rate_clipped", "rate_clipped", "acceleration_clipped"])
def test_recorded_clipping_changed_receipt_flag_is_rejected_after_independent_tracking_math(flag):
    data = list(tracking_fixture())
    check._tracking(*data)
    data[0][flag] = not data[0][flag]
    with pytest.raises(ValueError, match="governor_clipping_arithmetic"):
        check._tracking(*data)


@pytest.mark.parametrize("axis", [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.1, 0.9, 0.4]])
def test_conditioned_roll_transport_and_reacquisition_is_only_request_geometry(axis):
    profile, _, _, state, snapshot, _, _ = PUBLIC["fixture"]()
    previous = snapshot["context"]["conditioned_reference"]
    previous["bridging"] = True
    frame = ConditionedGeographicFrame(previous["quaternion"], previous["time_s"],
        maximum_roll_rate_rad_s=previous["maximum_roll_rate_rad_s"])
    frame.bridging = True
    q = check._attitude(axis, [0.0, 1.0, 0.0])
    when = state.time_s + 0.4
    _, axes = check._tower(profile, when)
    desired = check._rotate(q, [0.0, 0.0, 1.0])
    preferred = check._attitude(desired, axes[0])
    expected = frame.target(desired, axes[0], preferred, time_s=when)
    result, diagnostic = check._conditioned(previous, q, when, profile)
    assert result["quaternion"] == pytest.approx(expected, abs=1e-10)
    assert result["bridging"] is frame.bridging
    assert set(diagnostic) == set(frame.diagnostics)
    for key, value in diagnostic.items():
        if type(value) in (list, float, int) and type(value) is not bool:
            assert value == pytest.approx(frame.diagnostics[key], abs=1e-10)
        else:
            assert value == frame.diagnostics[key]


@pytest.fixture(scope="module")
def closed_corpus():
    profile, catch, _, _, snapshot, prior, command = PUBLIC["fixture"]()
    tracker = ReferenceTracker(profile, snapshot["context"]["prior_command_reference"]["quaternion"], 99.9)
    snapshot["context"]["reference_tracker"] = shooting.saved(vars(tracker))
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    corpus, ledger = {}, {"bytes": 0}

    def persist(identifier, payload):
        payload = json.loads(json.dumps(payload, allow_nan=False))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        corpus[identifier] = payload
        ledger["bytes"] += len(raw)
        return {"artifact_id": identifier, "format": "json", "relative_path": identifier + ".json",
            "raw_json_sha256": check.digest(payload), "sha256": check.digest(payload),
            "bytes": len(raw), "persisted_before_analysis": True}

    persist.check_before_query = lambda: None
    persist.resource_status = lambda: {"free_bytes": 500 * 1024 * 1024,
        "maximum_stored_bytes": 100 * 1024 * 1024, "minimum_free_bytes": 400 * 1024 * 1024,
        "persisted_bytes": ledger["bytes"]}
    result = producer.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=persist, wall_deadline_monotonic_s=time.monotonic() + 120,
        source_map={"public-fixture-source.py": "a" * 64})
    result = json.loads(json.dumps(result, allow_nan=False))
    protocol = corpus[result["origin_artifact"]["artifact_id"]]
    return result, protocol, plan, snapshot, profile, catch, corpus


def test_closed_static_corpus_all_branch_governor_loads_no_feasibility_admission(closed_corpus):
    result, protocol, plan, snapshot, profile, catch, corpus = closed_corpus
    verdict = check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)
    if not verdict["passed"]:
        check._check(result, protocol, plan, snapshot, profile, catch, corpus, None, None, None)
    assert verdict["passed"], verdict
    assert verdict["hypothetical_nodes_checked"] == 303
    assert verdict["model_observers_invoked"] == verdict["physical_plant_integrations_performed"] == 0
    assert all(verdict[key] is False for key in ("source_authenticated", "past_source_anchors_bound",
        "compressed_file_bytes_independently_verified", "os_persistence_authenticated",
        "caller_resource_budget_arithmetic_checked", "finite_actuator_feasibility_established",
        "continuous_fuel_feasibility_established", "joint_reference_feasibility_established",
        "candidate_plant_admitted", "arrival_admitted", "support_admitted", "physical_execution"))


def rebind_record(record, corpus):
    fields = check._RECORD_V2 if record["schema"] == check.SCHEMA_V2 or "computation_completed" in record else check._RECORD
    raw = {k: v for k, v in record.items() if k in fields}
    corpus[record["raw_artifact"]["artifact_id"]] = deepcopy(raw)
    record["raw_artifact"]["raw_json_sha256"] = check.digest(raw)


@pytest.mark.parametrize("change", ["calls", "updates", "lines", "source_binding", "node_clock",
    "raw_goal", "governed_request", "conditioned_roll", "nominal_cone", "admission", "mass_convention", "extra"])
def test_closed_corpus_changed_budget_branch_history_or_admission_is_rejected(closed_corpus, change):
    result, protocol, plan, snapshot, profile, catch, corpus = deepcopy(closed_corpus)
    node = result["sweeps"][0]["nodes"][30]
    if change == "calls":
        result["counters"]["model_queries_completed"] -= 1
    elif change == "updates":
        result["counters"]["updates_attempted"] -= 1
    elif change == "lines":
        result["counters"]["line_search_queries"] += 1
    elif change == "source_binding":
        result["source_map_sha256"] = "f" * 64
    elif change == "node_clock":
        node["time_s"] += 0.1
    elif change == "raw_goal":
        node["raw_goal_q_body_to_eci"][1] += 0.01
    elif change == "governed_request":
        node["governed_request_q_body_to_eci"][1] += 0.01
    elif change == "conditioned_roll":
        node["conditioned_frame_diagnostics"]["roll_step_rad"] += 0.01
    elif change == "nominal_cone":
        node["nominal_force_cone_passed"] = not node["nominal_force_cone_passed"]
    elif change == "admission":
        result["candidate_plant_admitted"] = True
    elif change == "mass_convention":
        result["mass_kinematics_convention"] = "physical_actuator_spool_mass_flow"
    else:
        result["extra"] = True
    rebind_record(result, corpus)
    verdict = check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)
    assert not verdict["passed"], change


def test_partial_after_one_complete_node_keeps_raw_queries_and_counts_attempted_next_update(closed_corpus):
    _, _, plan, snapshot, profile, catch, _ = deepcopy(closed_corpus)
    corpus, ledger = {}, {"bytes": 0, "checks": 0}

    def persist(identifier, payload):
        payload = json.loads(json.dumps(payload, allow_nan=False))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        corpus[identifier] = payload
        ledger["bytes"] += len(raw)
        return {"artifact_id": identifier, "format": "json", "relative_path": identifier + ".json",
            "raw_json_sha256": check.digest(payload), "sha256": check.digest(payload),
            "bytes": len(raw), "persisted_before_analysis": True}

    def resource_check():
        ledger["checks"] += 1
        finished = any(row.get("kind") == "static_model_raw"
            and corpus[row["attempted"]["artifact_id"]]["stage"] == "governed_recheck"
            for row in corpus.values())
        if finished:
            raise ValueError("synthetic_resource_stop_before_next_node")

    persist.check_before_query = resource_check
    persist.resource_status = lambda: {"free_bytes": 500 * 1024 * 1024,
        "maximum_stored_bytes": 100 * 1024 * 1024, "minimum_free_bytes": 400 * 1024 * 1024,
        "persisted_bytes": ledger["bytes"]}
    result = producer.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=persist, wall_deadline_monotonic_s=time.monotonic() + 120,
        source_map={"public-fixture-source.py": "a" * 64})
    result = json.loads(json.dumps(result, allow_nan=False))
    protocol = corpus[result["origin_artifact"]["artifact_id"]]
    verdict = check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)
    if not verdict["passed"]:
        check._check(result, protocol, plan, snapshot, profile, catch, corpus, None, None, None)
    assert verdict["passed"], verdict
    assert result["failure"] == "ValueError"
    assert result["counters"]["model_queries_attempted"] == result["counters"]["model_queries_completed"]
    bases = sum(row.get("kind") == "static_model_attempted" and row["stage"] == "base" for row in corpus.values())
    assert result["counters"]["updates_attempted"] == bases + 1
    assert verdict["hypothetical_nodes_checked"] == 1


@pytest.mark.parametrize("rotation", [(0.0, 0.0), (1.87646e-16, -1e-18),
    (1e-300, -1e-300), (math.sqrt(math.ulp(1.0)), 0.0), (0.2, -0.3)])
def test_v2_continuous_increment_analytic_identity_and_tiny_rotations(rotation):
    x, y = rotation
    angle = math.hypot(x, y)
    expected = [math.cos(angle / 2),
        x / 2 if angle < 1e-14 else x * math.sin(angle / 2) / angle,
        y / 2 if angle < 1e-14 else y * math.sin(angle / 2) / angle, 0.0]
    result = check._increment_continuous([1.0, 0.0, 0.0, 0.0], x, y)
    assert result == pytest.approx(expected, abs=0.0, rel=3e-16)
    assert math.hypot(*result) == pytest.approx(1.0, abs=2e-16)
    if 0 < angle < 1e-14:
        assert result[1:3] != [0.0, 0.0]
        assert check._increment([1.0, 0.0, 0.0, 0.0], x, y) == [1.0, 0.0, 0.0, 0.0]


def test_v2_continuous_increment_composes_in_body_frame_without_producer_kernel(monkeypatch):
    monkeypatch.setattr(producer, "_increment_continuous", lambda *a: pytest.fail("independent kernel"))
    q = [math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)]
    angle = 0.4
    result = check._increment_continuous(q, angle, 0.0)
    expected = [q[0] * math.cos(angle / 2), q[0] * math.sin(angle / 2),
        q[3] * math.sin(angle / 2), q[3] * math.cos(angle / 2)]
    assert result == pytest.approx(expected, abs=2e-16)


def _source_only_corpus(monkeypatch, *, revision=2, negative=True, partial=False, transverse_scale=0.0,
                        low_force=False, failure_mode=None, branch_force=None):
    """Protocol/branch fixture: no configured-load model or trajectory calls.

    Load/reference proofs are isolated with explicit stubs here. Existing load
    arithmetic tests and the separate immutable recorded-corpus recheck cover
    those boundaries; this fixture exercises revision, branch, and accounting.
    """
    from src.runtime import starship_sixdof as dynamics

    def forbidden(*args, **kwargs):
        pytest.fail("no observer, derivatives, integrator, or real static model in source fixture")

    for name in ("observe", "derivatives", "step"):
        monkeypatch.setattr(dynamics, name, forbidden)
    profile, catch, _, _, snapshot, _, _ = PUBLIC["fixture"]()
    tracker = ReferenceTracker(profile, snapshot["state"]["q_body_to_eci"], 99.9)
    snapshot["context"]["reference_tracker"] = shooting.saved(vars(tracker))
    plan = {"duration_s": 0.5, "origin_context_sha256": check.digest(snapshot)}
    monkeypatch.setattr(producer, "evaluate_terminal_reference", lambda plan, when: {"time_s": when})
    monkeypatch.setattr(check, "verify_fixed_terminal_reference", lambda *a, **k:
        {"passed": True, "arithmetic_passed": True,
         "past_source_anchors_bound": k.get("prior_checkpoint") is not None and k.get("source_run") is not None})
    monkeypatch.setattr(check, "_load", lambda item, *a: item["required_main_force_body_n"])
    corpus, ledger = {}, {"bytes": 0}

    def persist(identifier, payload):
        if payload["kind"] == "static_model_failed" and failure_mode in (
                "model_persist", "implementation_persist", "branch_model_persist"):
            exception = ValueError if failure_mode == "implementation_persist" else OSError
            raise exception("synthetic_failure_record_persistence")
        if payload["kind"] == "static_model_raw" and failure_mode == "raw_persist":
            raise OSError("synthetic_raw_record_persistence")
        if payload["kind"] == "static_model_attempted" and failure_mode == "attempt_persist":
            raise OSError("synthetic_attempt_record_persistence")
        payload = json.loads(json.dumps(payload, allow_nan=False))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        corpus[identifier] = payload
        ledger["bytes"] += len(raw)
        return {"artifact_id": identifier, "format": "json", "relative_path": identifier + ".json",
            "raw_json_sha256": check.digest(payload), "sha256": check.digest(payload),
            "bytes": len(raw), "persisted_before_analysis": True}

    def resource_check():
        if partial and any(row.get("kind") == "static_model_raw"
                and corpus[row["attempted"]["artifact_id"]]["stage"] == "governed_recheck"
                for row in corpus.values()):
            raise producer.StaticArtifactLimit("synthetic_source_fixture_partial_stop")
        if failure_mode == "budget" and any(row.get("kind") == "static_model_raw" for row in corpus.values()):
            raise producer._Budget()

    persist.check_before_query = resource_check
    persist.resource_status = lambda: {"free_bytes": 500 * 1024 * 1024,
        "maximum_stored_bytes": 100 * 1024 * 1024, "minimum_free_bytes": 400 * 1024 * 1024,
        "persisted_bytes": ledger["bytes"]}

    def source_fixture(sample, q, rate, acceleration, fuel, initial, body, profile, catch):
        stage = corpus[next(reversed(corpus))]["stage"]
        if failure_mode in ("model_persist", "implementation_persist", "model_recorded"):
            raise ArithmeticError("synthetic_model_failure")
        if stage == "positive_axial_branch_probe" and failure_mode in ("branch_model", "branch_model_persist"):
            raise ArithmeticError("synthetic_branch_probe_failure")
        mass = body.dry_mass_kg + fuel
        tolerance = math.sqrt(math.ulp(1.0)) * max(1.0, mass * 9.81)
        force = [0.0, 0.0, -tolerance / 2] if low_force else (
            [-transverse_scale * tolerance if transverse_scale else -1.397e-9, -1.819e-11, -16.655415]
            if negative else [0.0, 0.0, 1e6])
        if stage == "positive_axial_branch_probe" and branch_force == "positive":
            force = [0.0, 0.0, 1e6]
        elif stage == "positive_axial_branch_probe" and branch_force == "low":
            force = [0.0, 0.0, -tolerance / 2]
        elif branch_force == "negative_lines":
            angle = math.sqrt(math.ulp(1.0))
            force = {"base": [-1.0, 0.0, -16.655415],
                "positive_axial_branch_probe": [1.0, 1.0, 1e6],
                "jacobian_0": [1.0 + 100 * angle, 1.0, 1e6],
                "jacobian_1": [1.0, 1.0 + 100 * angle, 1e6],
                "governed_recheck": [0.0, 0.0, 1e6]}.get(stage, [0.1, 0.1, -1e6])
        values = {key: None for key in check._LOAD_FIELDS}
        values.update(mass_kg=mass, propellant_kg=fuel, required_main_force_body_n=force)
        return values

    monkeypatch.setattr(producer, "_static_query", source_fixture)
    if failure_mode == "geometry":
        def geometry_failure(*args, **kwargs):
            raise ValueError("synthetic_geometry_implementation_failure")
        monkeypatch.setattr(producer, "_attitude", geometry_failure)
    result = producer.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=persist, wall_deadline_monotonic_s=time.monotonic() + 120,
        source_map={"source-fixture.py": "a" * 64}, development_continuous_increment=revision >= 2,
        development_positive_axial_branch=revision == 3)
    result = json.loads(json.dumps(result, allow_nan=False))
    protocol = corpus[result["origin_artifact"]["artifact_id"]]
    return result, protocol, plan, snapshot, profile, catch, corpus


@pytest.mark.parametrize("revision,negative,partial", [(1, False, False), (2, False, False),
    (2, True, False), (2, True, True)])
def test_source_fixture_v1_unchanged_and_v2_positive_negative_partial_corpus(monkeypatch, revision, negative, partial):
    data = _source_only_corpus(monkeypatch, revision=revision, negative=negative, partial=partial)
    result = data[0]
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    if not verdict["passed"]:
        check._check(*data[:6], data[6], None, None, None)
    assert verdict["passed"], verdict
    nodes = result["current_partial_nodes"] if partial else [node for sweep in result["sweeps"] for node in sweep["nodes"]]
    assert verdict["hypothetical_nodes_checked"] == len(nodes) == (1 if partial else 303)
    assert result["counters"]["line_search_queries"] == 0
    assert result["counters"]["updates_attempted"] == len(nodes) + int(partial)
    assert result["counters"]["model_queries_completed"] == 2 * len(nodes)
    assert verdict["arithmetic_passed"] is verdict["arithmetic_record_integrity_passed"] is True
    assert verdict["source_bound_arithmetic_passed"] is False
    assert verdict["computation_completed"] is (not partial)
    assert all(verdict[key] is False for key in ("candidate_plant_admitted", "arrival_admitted", "support_admitted",
        "physical_invocation_admitted"))
    if negative:
        assert all(node["inverse_converged"] is False and node["low_force_anchor_unresolved"] is False
            and node["nominal_force_cone_passed"] is False for node in nodes)
        assert all(node["updates"][0]["status"] == "transverse_stationary_negative_axial" for node in nodes)
        assert not any(request.get("stage", "").startswith(("jacobian_", "line_search_"))
            for request in data[6].values())
        assert all(node["raw_goal_q_body_to_eci"] == nodes[0]["raw_goal_q_body_to_eci"] for node in nodes)
    else:
        assert all(node["inverse_converged"] is True and node["updates"] == [] for node in nodes)


@pytest.mark.parametrize("transverse_scale,low_force,status,query_factor", [
    (0.99, False, "transverse_stationary_negative_axial", 2),
    (1.01, False, "singular_transverse_jacobian", 4),
    (0.0, True, None, 2)])
def test_source_fixture_v2_keeps_existing_transverse_tolerance_and_low_force_precedence(
        monkeypatch, transverse_scale, low_force, status, query_factor):
    data = _source_only_corpus(monkeypatch, transverse_scale=transverse_scale, low_force=low_force)
    result = data[0]
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    assert verdict["passed"], verdict
    nodes = [node for sweep in result["sweeps"] for node in sweep["nodes"]]
    assert result["counters"]["model_queries_completed"] == query_factor * len(nodes)
    assert all(node["inverse_converged"] is False and node["low_force_anchor_unresolved"] is low_force
        for node in nodes)
    if status is None:
        assert all(node["updates"] == [] for node in nodes)
    else:
        assert all(node["updates"][0]["status"] == status for node in nodes)


@pytest.mark.parametrize("change", ["record_revision", "protocol_revision", "configuration_revision",
    "kernel_source", "branch_source", "threshold", "updates", "lines", "branch", "convergence",
    "positive_axial", "inverse_claim", "cone"])
def test_source_fixture_v2_rejects_revision_mixing_branch_or_counter_rewrites(monkeypatch, change):
    result, protocol, plan, snapshot, profile, catch, corpus = _source_only_corpus(monkeypatch)
    node = result["sweeps"][0]["nodes"][0]
    if change == "record_revision":
        result["schema"] = check.SCHEMA
    elif change == "protocol_revision":
        protocol["schema"] = check.SCHEMA
    elif change == "configuration_revision":
        protocol["configuration"] = deepcopy(check.CONFIG)
    elif change in ("kernel_source", "branch_source"):
        key = "newton_increment_source" if change == "kernel_source" else "negative_axial_stationary_source"
        protocol["configuration"][key] = "unknown_revision"
    elif change == "threshold":
        protocol["configuration"]["negative_axial_tolerance_n"] = 20.0
    elif change in ("updates", "lines"):
        result["counters"]["updates_attempted" if change == "updates" else "line_search_queries"] += 1
    elif change == "branch":
        node["updates"] = []
    elif change == "convergence":
        node["inverse_converged"] = True
    elif change in ("positive_axial", "inverse_claim"):
        node["updates"][0]["positive_axial_passed" if change == "positive_axial" else "inverse_convergence_established"] = True
    else:
        node["nominal_force_cone_passed"] = True
    corpus[result["origin_artifact"]["artifact_id"]] = deepcopy(protocol)
    result["origin_artifact"]["raw_json_sha256"] = check.digest(protocol)
    rebind_record(result, corpus)
    assert not check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)["passed"], change


@pytest.mark.parametrize("failure_mode,category,stage,completed,failed,incomplete", [
    ("model_persist", "resource_or_io", "failed_model_record_persistence", 0, 1, "model_failed_before_failure_persist"),
    ("implementation_persist", "implementation", "failed_model_record_persistence", 0, 1, "model_failed_before_failure_persist"),
    ("model_recorded", "recorded_model_failure", "static_model_evaluation", 0, 1, None),
    ("raw_persist", "resource_or_io", "raw_model_record_persistence", 1, 0, "model_completed_before_raw_persist"),
    ("attempt_persist", "resource_or_io", "attempt_record_persistence", 0, 0, None),
    ("geometry", "implementation", "reference_geometry_or_iteration", 1, 0, None),
    ("budget", "budget", "static_budget_guard", 1, 0, None)])
def test_source_fixture_v2_retains_original_failure_and_separates_integrity_from_completion(
        monkeypatch, failure_mode, category, stage, completed, failed, incomplete):
    data = _source_only_corpus(monkeypatch, failure_mode=failure_mode)
    result = data[0]
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    if not verdict["passed"]:
        check._check(*data[:6], data[6], None, None, None)
    assert verdict["passed"] is verdict["arithmetic_record_integrity_passed"] is True
    assert result["computation_completed"] is verdict["computation_completed"] is False
    assert result["failure_category"] == verdict["failure_category"] == category
    assert result["failure_stage"] == verdict["failure_stage"] == stage
    assert result["counters"]["model_queries_completed"] == completed
    assert result["counters"]["model_queries_failed"] == failed
    assert (result["incomplete_io"]["stage"] if result["incomplete_io"] else None) == incomplete
    if incomplete == "model_failed_before_failure_persist":
        assert result["incomplete_io"]["error_class"] == "ArithmeticError"
        assert result["failure"] == ("OSError" if failure_mode == "model_persist" else "ValueError")
        assert not any(row.get("kind") == "static_model_failed" for row in data[6].values())
    assert all(verdict[key] is False for key in ("physical_invocation_admitted", "candidate_plant_admitted",
        "arrival_admitted", "support_admitted", "physical_execution"))


@pytest.mark.parametrize("change", ["missing_original_error", "changed_request", "failed_counter", "category_budget",
    "category_recorded", "stage_geometry", "computation_complete", "extra_field"])
def test_source_fixture_v2_failed_model_persistence_rejects_loss_or_relabeling(monkeypatch, change):
    result, protocol, plan, snapshot, profile, catch, corpus = _source_only_corpus(monkeypatch, failure_mode="model_persist")
    if change == "missing_original_error":
        del result["incomplete_io"]["error_class"]
    elif change == "changed_request":
        result["incomplete_io"]["request"]["index"] += 1
    elif change == "failed_counter":
        result["counters"]["model_queries_failed"] = 0
    elif change in ("category_budget", "category_recorded"):
        result["failure_category"] = "budget" if change == "category_budget" else "recorded_model_failure"
    elif change == "stage_geometry":
        result["failure_stage"] = "reference_geometry_or_iteration"
    elif change == "computation_complete":
        result["computation_completed"] = True
    else:
        result["incomplete_io"]["raw_error_erased"] = True
    rebind_record(result, corpus)
    verdict = check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch, query_corpus=corpus)
    assert verdict["passed"] is verdict["arithmetic_record_integrity_passed"] is False
    assert verdict["computation_completed"] is False


@pytest.mark.parametrize("change", ["category", "stage", "missing_completion", "false_completion"])
def test_source_fixture_v2_complete_record_requires_consistent_completion_fields(monkeypatch, change):
    result, protocol, plan, snapshot, profile, catch, corpus = _source_only_corpus(monkeypatch, negative=False)
    if change == "category":
        result["failure_category"] = "budget"
    elif change == "stage":
        result["failure_stage"] = "static_budget_guard"
    elif change == "missing_completion":
        del result["computation_completed"]
    else:
        result["computation_completed"] = False
    rebind_record(result, corpus)
    assert not check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)["arithmetic_passed"]


@pytest.mark.parametrize("anchors,expected_map,partial,expected", [(False, True, False, False),
    (True, False, False, False), (True, True, False, True), (True, True, True, True)])
def test_source_fixture_strict_caller_requires_retained_anchors_and_explicit_source_map(
        monkeypatch, anchors, expected_map, partial, expected):
    data = _source_only_corpus(monkeypatch, negative=False, partial=partial)
    kwargs = {"query_corpus": data[6], "prior_checkpoint": {} if anchors else None,
        "source_run": {} if anchors else None,
        "expected_source_map": data[1]["source_map"] if expected_map else None}
    arithmetic = check.verify_coupled_terminal_reference(*data[:6], **kwargs)
    strict = check.verify_source_bound_coupled_terminal_reference(*data[:6], **kwargs)
    assert arithmetic["arithmetic_passed"] is arithmetic["passed"] is True
    assert arithmetic["source_bound_arithmetic_passed"] is strict["passed"] is expected
    assert strict["arithmetic_record_integrity_passed"] is True
    assert strict["computation_completed"] is (not partial)
    assert strict["physical_invocation_admitted"] is strict["source_authenticated"] is False


@pytest.mark.parametrize("negative,branch_force,low_force,partial,probes,applied", [
    (False, None, False, False, 0, False), (True, "positive", False, False, 303, True),
    (True, "positive", False, True, 1, True), (True, None, False, True, 1, False),
    (True, "low", False, True, 1, False), (True, "positive", True, True, 0, False)])
def test_source_fixture_v3_closed_positive_and_unresolved_branch_corpora(
        monkeypatch, negative, branch_force, low_force, partial, probes, applied):
    data = _source_only_corpus(monkeypatch, revision=3, negative=negative, branch_force=branch_force,
        low_force=low_force, partial=partial)
    result = data[0]
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    if not verdict["passed"]:
        check._check(*data[:6], data[6], None, None, None)
    assert verdict["arithmetic_passed"] is True, verdict
    assert verdict["computation_completed"] is (not partial)
    assert verdict["hypothetical_nodes_checked"] == (1 if partial else 303)
    assert result["schema"] == check.SCHEMA_V3
    assert result["counters"]["positive_axial_branch_probes"] == probes
    assert data[1]["configuration"]["maximum_model_queries"] == 17574
    assert data[1]["configuration"]["maximum_positive_axial_branch_probes"] == 303
    nodes = result["current_partial_nodes"] if partial else [n for s in result["sweeps"] for n in s["nodes"]]
    for node in nodes:
        branch = node["positive_axial_branch"]
        assert branch["physical_state_assigned"] is False
        assert branch["applied"] is applied
        if branch["attempted"]:
            raw = data[6][branch["from_query"]["artifact_id"]]
            before = data[6][raw["attempted"]["artifact_id"]]
            probe = data[6][branch["probe_query"]["artifact_id"]]
            request = data[6][probe["attempted"]["artifact_id"]]
            assert request["stage"] == "positive_axial_branch_probe"
            assert request["q"] == pytest.approx(check._increment_continuous(before["q"], 0.0, math.pi), abs=1e-10)
            assert request["index"] > before["index"]
        if applied:
            assert node["inverse_converged"] is True
        elif negative:
            assert node["inverse_converged"] is False
    assert all(verdict[k] is False for k in ("physical_invocation_admitted", "candidate_plant_admitted",
        "arrival_admitted", "support_admitted", "joint_reference_feasibility_established"))


def test_source_fixture_v3_rejects_negative_axial_lines_even_with_lower_transverse_residual(monkeypatch):
    data = _source_only_corpus(monkeypatch, revision=3, branch_force="negative_lines", partial=True)
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    assert verdict["arithmetic_record_integrity_passed"] is True, verdict
    node = data[0]["current_partial_nodes"][0]
    assert node["positive_axial_branch"]["applied"] is True
    assert node["inverse_converged"] is False
    assert node["updates"][0]["status"] == "line_search_no_decrease"
    assert len(node["updates"][0]["line_search_queries"]) == data[0]["counters"]["line_search_queries"] == 4
    assert data[0]["counters"]["positive_axial_branch_probes"] == 1


@pytest.mark.parametrize("failure_mode", ["branch_model", "branch_model_persist"])
def test_source_fixture_v3_failed_probe_keeps_consumed_branch_and_model_error(monkeypatch, failure_mode):
    data = _source_only_corpus(monkeypatch, revision=3, failure_mode=failure_mode)
    result = data[0]
    verdict = check.verify_coupled_terminal_reference(*data[:6], query_corpus=data[6])
    if not verdict["passed"]:
        check._check(*data[:6], data[6], None, None, None)
    assert verdict["arithmetic_record_integrity_passed"] is True, verdict
    assert verdict["computation_completed"] is False
    assert result["counters"]["positive_axial_branch_probes"] == 1
    assert result["counters"]["model_queries_attempted"] == 2
    assert result["counters"]["model_queries_completed"] == result["counters"]["model_queries_failed"] == 1
    if failure_mode == "branch_model_persist":
        assert result["incomplete_io"]["error_class"] == "ArithmeticError"
        assert result["incomplete_io"]["request"]["stage"] == "positive_axial_branch_probe"


@pytest.mark.parametrize("change", ["attempted", "from_query", "probe_query", "positive_axial", "applied",
    "source", "actual_assignment", "branch_counter", "branch_cap", "query_cap", "schema_mixing", "probe_q", "hidden_probe"])
def test_source_fixture_v3_branch_receipt_budget_input_or_schema_mutations_fail(monkeypatch, change):
    result, protocol, plan, snapshot, profile, catch, corpus = _source_only_corpus(
        monkeypatch, revision=3, branch_force="positive", partial=True)
    node = result["current_partial_nodes"][0]
    branch = node["positive_axial_branch"]
    if change == "attempted":
        branch["attempted"] = False
    elif change == "from_query":
        branch["from_query"] = deepcopy(branch["probe_query"])
    elif change == "probe_query":
        branch["probe_query"] = deepcopy(branch["from_query"])
    elif change == "positive_axial":
        branch["positive_axial_passed"] = False
    elif change == "applied":
        branch["applied"] = False
    elif change == "source":
        branch["proposal_source"] = "other_axis_or_angle_search"
    elif change == "actual_assignment":
        branch["physical_state_assigned"] = True
    elif change == "branch_counter":
        result["counters"]["positive_axial_branch_probes"] += 2
    elif change in ("branch_cap", "query_cap"):
        protocol["configuration"]["maximum_positive_axial_branch_probes" if change == "branch_cap" else "maximum_model_queries"] += 1
    elif change == "schema_mixing":
        result["schema"] = protocol["schema"] = check.SCHEMA_V2
        protocol["configuration"] = deepcopy(check.CONFIG_V2)
    elif change == "probe_q":
        envelope = corpus[branch["probe_query"]["artifact_id"]]
        request = corpus[envelope["attempted"]["artifact_id"]]
        request["q"] = [1.0, 0.0, 0.0, 0.0]
        envelope["attempted"]["raw_json_sha256"] = check.digest(request)
        for manifest in result["query_artifacts"]:
            if manifest["artifact_id"] == branch["probe_query"]["artifact_id"]:
                manifest["raw_json_sha256"] = check.digest(envelope)
    else:
        result["query_artifacts"] = [m for m in result["query_artifacts"] if m["artifact_id"] != branch["probe_query"]["artifact_id"]]
    corpus[result["origin_artifact"]["artifact_id"]] = deepcopy(protocol)
    result["origin_artifact"]["raw_json_sha256"] = check.digest(protocol)
    rebind_record(result, corpus)
    assert not check.verify_coupled_terminal_reference(result, protocol, plan, snapshot, profile, catch,
        query_corpus=corpus)["arithmetic_record_integrity_passed"], change


def test_source_fixture_v3_duplicate_probe_node_is_rejected_with_unique_artifacts(monkeypatch):
    result, protocol, plan, snapshot, profile, catch, corpus = _source_only_corpus(
        monkeypatch, revision=3, branch_force="positive")
    probes = [m for m in result["query_artifacts"]
        if corpus[corpus[m["artifact_id"]]["attempted"]["artifact_id"]]["stage"] == "positive_axial_branch_probe"]
    envelope = corpus[probes[1]["artifact_id"]]
    request = corpus[envelope["attempted"]["artifact_id"]]
    request["node"] = 0
    envelope["attempted"]["raw_json_sha256"] = check.digest(request)
    probes[1]["raw_json_sha256"] = check.digest(envelope)
    rebind_record(result, corpus)
    with pytest.raises(ValueError, match="one_durable_body_pitch_pi_probe_per_node_and_attempt_counter"):
        check._check(result, protocol, plan, snapshot, profile, catch, corpus, None, None, None)
