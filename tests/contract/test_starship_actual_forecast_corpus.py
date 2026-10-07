"""External corpus integrity fixtures; stored predictions are not execution.

Synthetic traces test source and observation binding. One opt-in-free public
plant test advances a single .1-second step; no full return is run here.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import gzip
from hashlib import sha256
import json
from pathlib import Path

import pytest

from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_actual_forecast_corpus as corpus
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_booster_catch import _initialized, configuration
from src.runtime.starship_booster_recovery import _tower_observation
from src.runtime.starship_constrained_recovery import physical_guidance_configuration
from src.runtime.starship_sixdof_mission import _attitude, vehicle


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sink(root):
    def persist(identity, payload):
        raw = canonical(payload)
        compressed = payload["kind"] == "raw_forecast"
        kind = "json.gz" if compressed else "json"
        stored = gzip.compress(raw, mtime=0) if compressed else raw
        name = identity+"."+kind
        with (root/name).open("xb") as file:
            file.write(stored)
        return {"artifact_id": identity, "format": kind, "relative_path": name,
                "sha256": sha256(stored).hexdigest(), "raw_json_sha256": sha256(raw).hexdigest(),
                "bytes": len(stored), "persisted_before_analysis": True}
    return persist


def fixture(*, public_short=False, transported=False):
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = configuration(json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text()))
    if public_short:
        body = vehicle(profile, "booster")
        point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 30000., time_s=100.)
        up, east, _ = env.local_frame(point)
        state = dyn.State6DOF(100., point.r, env.add(point.v, env.scale(up, -50.)), _attitude(up, east),
            (0., 0., 0.), 78000., tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
    else:
        body, state = _initialized(profile, catch, "booster_catch")
    plan = {"burn_axis_enu": [0., 0., 1.], "target_velocity_enu_mps": [0., 0., -50.],
            "bank_heading_enu": [1., 0., 0.], "bank_sign": -1, "prediction_is_execution": False}
    reference = ConditionedGeographicFrame(state.q_body_to_eci, state.time_s, maximum_roll_rate_rad_s=.03/.28)
    physical_config = physical_guidance_configuration(transported)
    snapshot = shooting.capture_context(state, profile, catch, physical_config,
        phase="recovery_boostback_slew", start_time_s=state.time_s,
        deadline_s=state.time_s+(.1 if public_short else 1200.), plan=plan, refreshed=True,
        burn_start_s=None, settle_start_s=None, previous_entry_axis_enu=None,
        entry_pretrim_prepared_at_s=None, next_preview_s=state.time_s, braking_preview=None,
        prior_command_reference={"quaternion": list(state.q_body_to_eci), "time_s": state.time_s},
        conditioned_reference=reference, reference_tracker=None,
        **({"prepare_tail_start_s": None} if transported else {}))
    return profile, catch, body, state, snapshot


def synthetic_run(profile, catch, body, state, snapshot, request):
    """Saved arithmetic fixture, not an integrated or executable flight."""
    final = replace(state, time_s=state.time_s+.1)
    observation = shooting.saved(_tower_observation(final, body, profile, catch))
    first = {"time_s": state.time_s, "phase": "recovery_boostback_slew", "state": shooting.saved(asdict(state)),
             "command": None, "com_rate_body_mps": [0., 0., 0.], "navigation": {}}
    last = {"time_s": final.time_s, "phase": "recovery_landing_13", "state": shooting.saved(asdict(final)),
            "command": None, "com_rate_body_mps": [0., 0., 0.], "navigation": {"arrival_observation": observation}}
    final_context = deepcopy(snapshot)
    final_context["state"] = last["state"]
    transported = snapshot["schema"] == shooting.TRANSPORT_CONTEXT_SCHEMA
    return {"guidance_policy": "constrained_return_development_v10" if transported else "constrained_return_development_v8", "initial_state": first["state"],
        "booster_separation_state": first["state"],
        "final_state": last["state"], "contact": None, "final_controller_context": final_context,
        "recovery_record": {"input_separation_state": first["state"], "checkpoints": [first, last],
                            "guidance_configuration": physical_guidance_configuration(transported),
                            "handoff": {"eligible": False, "observation": observation}},
        "forecast_metadata": {"schema": "missionos.starship_actual_recovery_forecast.v1",
            "origin_context_sha256": request["origin_context_sha256"], "request": request,
            "prediction_complete": True, "failure": None, "prediction_is_execution": False,
            "production_policy_admitted": False},
        "outcome": {"termination": "surface_contact", "integration_steps": 1}, "synthetic_fixture": True}


def build(root, monkeypatch, *, baseline=True, failed=False, terminal=False, context_failed=False, transported=False):
    profile, catch, body, state, snapshot = fixture(transported=transported)
    baseline_request = {"origin_context_sha256": shooting.digest(snapshot), "duration_s": 600.,
                        "maximum_integration_steps": 6500, "wall_deadline_monotonic_s": 9999999.}
    reference = synthetic_run(profile, catch, body, state, snapshot, baseline_request) if baseline else None
    if terminal and reference:
        reference["recovery_record"]["checkpoints"][-1]["navigation"] = {}
    calls = []
    def forecast(origin, cfg, recovery, candidate, request):
        calls.append(1)
        if failed and len(calls) == 2:
            raise ArithmeticError("synthetic failure")
        candidate_origin = deepcopy(origin)
        candidate_origin["context"]["plan"] = candidate
        run = synthetic_run(profile, catch, body, state, candidate_origin, request)
        if terminal:
            run["recovery_record"]["checkpoints"][-1]["navigation"] = {}
        if context_failed:
            run["final_controller_context"] = None
            run["forecast_metadata"].update(failure="RuntimeError", final_context_error="RuntimeError",
                                           prediction_complete=False)
        return run
    monkeypatch.setattr(shooting, "forecast_constrained_continuation", forecast)
    binding = {"run_sha256": shooting.digest(reference), "profile_sha256": shooting.digest(profile),
               "catch_profile_sha256": shooting.digest(catch)} if transported else None
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
        artifact_sink=sink(root), baseline_reference=reference, baseline_binding=binding)
    return receipt, reference


def rewrite(root, manifest, update):
    path = root/manifest["relative_path"]
    stored = path.read_bytes()
    payload = json.loads(gzip.decompress(stored) if manifest["format"] == "json.gz" else stored)
    update(payload)
    raw = canonical(payload)
    changed = gzip.compress(raw, mtime=0) if manifest["format"] == "json.gz" else raw
    path.write_bytes(changed)
    manifest.update(sha256=sha256(changed).hexdigest(), raw_json_sha256=sha256(raw).hexdigest(), bytes=len(changed))


@pytest.mark.parametrize("terminal", [False, True])
def test_canonical_corpus_binds_nonzero_candidates_best_actual_observation_and_baseline(tmp_path, monkeypatch, terminal):
    receipt, baseline = build(tmp_path, monkeypatch, terminal=terminal)
    before = deepcopy(receipt)
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline,
                                           source_sha256={"fixture.py": "a"*64})
    assert result["passed"], result
    assert result["artifact_count"] == 1+2*receipt["attempted_forecast_count"]
    assert result["same_time_score_bound_to_raw_verified"] is True
    assert result["baseline_equivalence_verified"] is True
    assert result["baseline_matched"] is True
    assert result["source_binding_verified"] is False
    assert result["persistence_before_analysis_independently_verified"] is False
    assert result["runtime_invocation_independently_verified"] is False
    assert result["model_value_established"] is False
    assert result["physical_execution"] is False
    assert receipt == before


def test_failed_attempt_keeps_external_raw_failure_and_consumed_call(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch, failed=True)
    assert receipt["failed_forecast_count"] == 1
    assert corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]
    entry = receipt["forecasts"][1]
    rewrite(tmp_path, entry["raw_artifact"], lambda payload: payload.update(error_type="OtherError"))
    assert not corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]


def test_missing_baseline_stays_reference_failure_without_claiming_model_value(tmp_path, monkeypatch):
    receipt, _ = build(tmp_path, monkeypatch, baseline=False)
    result = corpus.verify_forecast_corpus(tmp_path, receipt)
    assert result["passed"], result
    assert result["baseline_matched"] is False
    assert receipt["attempted_forecast_count"] == 1


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json", "forecasts/nested.json",
                                  "..\\outside.json", "file:outside.json", "./same.json"])
def test_external_manifest_path_is_exact_opaque_basename(tmp_path, monkeypatch, path):
    receipt, baseline = build(tmp_path, monkeypatch)
    receipt["origin_artifact"]["relative_path"] = path
    assert not corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]


@pytest.mark.parametrize("change", ["symlink", "root_symlink", "extra_file", "extra_directory", "missing", "duplicate"])
def test_missing_extra_or_symlink_corpus_never_verifies(tmp_path, monkeypatch, change):
    root = tmp_path/"corpus"
    root.mkdir()
    receipt, baseline = build(root, monkeypatch)
    path = root/receipt["origin_artifact"]["relative_path"]
    if change == "symlink":
        other = tmp_path/"outside.json"
        path.replace(other)
        path.symlink_to(other)
    elif change == "root_symlink":
        link = tmp_path/"link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif change == "extra_file":
        (root/"extra.json").write_text("{}")
    elif change == "extra_directory":
        (root/"other").mkdir()
    elif change == "missing":
        path.unlink()
    else:
        receipt["forecasts"][0]["attempted_artifact"] = receipt["origin_artifact"]
    assert not corpus.verify_forecast_corpus(root, receipt, baseline_reference=baseline)["passed"]


@pytest.mark.parametrize("change", ["bytes", "stored_hash", "raw_hash", "noncanonical", "duplicate_key", "nonfinite"])
def test_bytes_hashes_and_canonical_json_are_all_required(tmp_path, monkeypatch, change):
    receipt, baseline = build(tmp_path, monkeypatch)
    manifest = receipt["origin_artifact"]
    path = tmp_path/manifest["relative_path"]
    if change == "bytes":
        manifest["bytes"] += 1
    elif change == "stored_hash":
        manifest["sha256"] = "f"*64
    elif change == "raw_hash":
        manifest["raw_json_sha256"] = "f"*64
    else:
        raw = path.read_bytes()
        if change == "noncanonical":
            raw += b"\n"
        elif change == "duplicate_key":
            raw = raw[:-1]+b',"kind":"origin_context"}'
        else:
            raw = raw[:-1]+b',"nonfinite":NaN}'
        path.write_bytes(raw)
        manifest.update(bytes=len(raw), sha256=sha256(raw).hexdigest(), raw_json_sha256=sha256(raw).hexdigest())
    assert not corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]


@pytest.mark.parametrize("change", ["origin", "attempt", "raw_request", "status", "count", "candidate", "score_cp", "score_obs"])
def test_correctly_rehashed_files_cannot_escape_input_disposition_or_score_binding(tmp_path, monkeypatch, change):
    receipt, baseline = build(tmp_path, monkeypatch)
    entry = receipt["forecasts"][0]
    if change == "origin":
        rewrite(tmp_path, receipt["origin_artifact"], lambda payload: payload["snapshot"]["context"].update(refreshed=False))
    elif change == "attempt":
        rewrite(tmp_path, entry["attempted_artifact"], lambda payload: payload.update(parameters=[.002, 0., 0.]))
    elif change == "raw_request":
        rewrite(tmp_path, entry["raw_artifact"], lambda payload: payload["request"].update(duration_s=599.))
    elif change == "status":
        entry["status"] = "partial"
    elif change == "count":
        receipt["completed_forecast_count"] -= 1
    elif change == "candidate":
        entry["candidate_request"]["burn_axis_enu"] = [1., 0., 0.]
    elif change == "score_cp":
        entry["score"]["checkpoint"]["state"]["propellant_kg"] -= 1.
        entry["score"]["checkpoint_sha256"] = shooting.digest(entry["score"]["checkpoint"])
    else:
        entry["score"]["actual_same_time_observation"]["position_error_enu_m"][0] += 1.
    assert not corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]


def test_changed_external_baseline_is_not_an_optimization_objective(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch)
    baseline["recovery_record"]["checkpoints"][-1]["state"]["v_eci_mps"][0] += .1
    assert not corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)["passed"]


def test_score_must_match_the_saved_raw_checkpoint_even_when_compact_digest_is_valid(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch)
    entry = receipt["forecasts"][0]
    rewrite(tmp_path, entry["raw_artifact"], lambda payload: payload["forecast"]["recovery_record"]["checkpoints"][-1]["navigation"].update(extra="changed"))
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)
    assert not result["passed"]
    assert result["issues"] == ["score_not_bound_to_best_raw_checkpoint"]


def test_one_public_actual_step_is_persisted_and_partial_not_selectable(tmp_path):
    profile, catch, _, _, snapshot = fixture(public_short=True)
    _, receipt = shooting.refine_actual_recovery_plan(snapshot, profile, catch, snapshot["context"]["plan"],
        artifact_sink=sink(tmp_path))
    entry = receipt["forecasts"][0]
    assert entry["integration_steps"] == 1
    assert entry["status"] == "partial"
    assert entry["score"] is None
    result = corpus.verify_forecast_corpus(tmp_path, receipt)
    assert result["passed"], result
    assert result["dynamics_replayed"] is False
    assert result["model_value_established"] is False


def test_selected_prediction_binds_entire_synthetic_execution_suffix(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch)
    entry = receipt["forecasts"][receipt["selected_attempt_index"]-1]
    raw = gzip.decompress((tmp_path/entry["raw_artifact"]["relative_path"]).read_bytes())
    executed = json.loads(raw)["forecast"]
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline, executed_run=executed)
    assert result["passed"], result
    assert result["execution_comparison_applicable"] is True
    assert result["selected_complete_forecast_matches_execution"] is True
    assert result["runtime_invocation_independently_verified"] is False
    assert result["arrival_admitted"] is False


def test_transported_schema3_uses_matching_context_and_baseline_binding(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch, transported=True)
    entry = receipt["forecasts"][receipt["selected_attempt_index"]-1]
    executed = json.loads(gzip.decompress((tmp_path/entry["raw_artifact"]["relative_path"]).read_bytes()))["forecast"]
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline, executed_run=executed)
    assert result["passed"], result
    assert receipt["schema"] == "missionos.starship_actual_recovery_shooting.v3"
    assert result["baseline_matched"] is True
    assert result["selected_complete_forecast_matches_execution"] is True
    assert result["source_binding_verified"] is False


@pytest.mark.parametrize("field", ["run_sha256", "profile_sha256", "catch_profile_sha256"])
def test_rehashed_transported_origin_cannot_change_the_baseline_inputs(tmp_path, monkeypatch, field):
    receipt, baseline = build(tmp_path, monkeypatch, transported=True)
    receipt["baseline_binding"][field] = "f"*64
    rewrite(tmp_path, receipt["origin_artifact"],
            lambda payload: payload["baseline_binding"].update({field: "f"*64}))
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)
    assert not result["passed"]
    assert result["issues"] == ["transported_baseline_binding_mismatch"]


def test_final_context_failure_preserves_raw_trace_without_a_selectable_score(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch, context_failed=True)
    assert receipt["attempted_forecast_count"] == receipt["failed_forecast_count"] == 1
    assert receipt["forecasts"][0]["score"] is None
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)
    assert result["passed"], result
    assert result["selected_complete_forecast_matches_execution"] is False


def test_declared_handoff_without_terminal_gate_or_event_is_rejected(tmp_path, monkeypatch):
    receipt, baseline = build(tmp_path, monkeypatch)
    entry = receipt["forecasts"][0]
    entry["handoff_eligible"] = True
    rewrite(tmp_path, entry["raw_artifact"], lambda payload: payload["forecast"]["recovery_record"]["handoff"].update(eligible=True))
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline)
    assert not result["passed"]
    assert result["issues"] == ["raw_handoff_not_bound_to_actual_terminal_gate"]


@pytest.mark.parametrize("field", ["state", "phase", "clock", "com_rate", "missing_checkpoint"])
def test_selected_prediction_refuses_a_different_synthetic_execution(tmp_path, monkeypatch, field):
    receipt, baseline = build(tmp_path, monkeypatch)
    entry = receipt["forecasts"][receipt["selected_attempt_index"]-1]
    raw = gzip.decompress((tmp_path/entry["raw_artifact"]["relative_path"]).read_bytes())
    executed = json.loads(raw)["forecast"]
    point = executed["recovery_record"]["checkpoints"][-1]
    if field == "state":
        point["state"]["propellant_kg"] -= .1
    elif field == "phase":
        point["phase"] = "recovery_entry_coast"
    elif field == "clock":
        point["time_s"] += .01
    elif field == "com_rate":
        point["com_rate_body_mps"][0] += .001
    else:
        executed["recovery_record"]["checkpoints"].pop()
    result = corpus.verify_forecast_corpus(tmp_path, receipt, baseline_reference=baseline, executed_run=executed)
    assert not result["passed"]
    assert result["issues"] == ["selected_forecast_execution_mismatch"]
