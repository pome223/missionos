#!/usr/bin/env python3
"""Fresh registration, opt-in execution and saved-only final mission checking.

No command executes by default. ``execute`` requires the exact reviewed plan
hash and a new run directory; stopped attempts cannot be resumed or retried.
Provider credentials must already be supplied to the host environment. They are
never arguments, saved inputs, worker environment or public evidence.
"""
from contextlib import contextmanager
from hashlib import sha256
import argparse
import gzip
import json
import os
from pathlib import Path
import sys
import time
import shutil
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"packages/missionos-core/src"), str(ROOT/"packages/missionos-cli/src"),
    str(ROOT/"packages/missionos-gateway/src")]

from src.runtime import starship_final_shared as shared  # noqa: E402
from src.runtime import starship_final_plan as planning  # noqa: E402

SCRIPT = "scripts/run_starship_final_mission.py"
MAXIMUM_RECORD_BYTES = 512*2**20


def output_path(name):
    path = shared.relative(name)
    shared.require(path.is_relative_to(ROOT/"output"), "final_private_output_must_be_under_output")
    return path


def read_raw(path, maximum=512*2**20):
    path = Path(path)
    shared.require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
        "final_artifact_missing_or_oversized")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        raw = stream.read(maximum+1)
    shared.require(len(raw) <= maximum, "final_artifact_raw_budget")
    return raw


def restore_record(path):
    packed = read_raw(path)
    from io import BytesIO
    with gzip.GzipFile(fileobj=BytesIO(packed)) as stream:
        raw = stream.read(MAXIMUM_RECORD_BYTES+1)
    shared.require(len(raw) <= MAXIMUM_RECORD_BYTES, "final_record_restore_budget")
    shared.parse(raw)
    return raw


@contextmanager
def saved_only():
    """Assert that verification cannot fly, spawn workers or call providers."""
    from src.runtime.starship_restored_outer import offline_worker
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    import subprocess
    def forbidden(*_a, **_kw):
        raise ValueError("final_saved_checker_cannot_execute")
    with offline_worker(), patch.object(dyn, "step", forbidden), patch.object(dyn, "_rk4", forbidden), \
            patch.object(dyn, "_rhs", forbidden), patch.object(env, "step", forbidden), \
            patch.object(subprocess, "Popen", forbidden):
        yield


def verify_saved(plan, folder):
    from src.runtime.starship_final_verifier import verify_final_mission
    from src.runtime.starship_restored_artifacts import load_restored_native_artifacts
    folder = Path(folder)
    raw = restore_record(folder/"record.json.gz")
    record = shared.parse(raw)
    receipt = shared.read(folder/"outer-receipt.json")
    host_raw = {channel: read_raw(folder/("host-"+channel+".json"))
        for channel in ("restored", "supplemental", "visual", "sensor")}
    artifacts = {} if record["governance"] is None else load_restored_native_artifacts(record["governance"], folder/"governance")
    artifacts[plan["input_sha256"][shared.PROFILE]] = read_raw(shared.relative(shared.PROFILE))
    refs = receipt["artifact_refs"]
    shared.require(type(refs) is dict and refs, "final_outer_artifact_refs_required")
    for name, identity in refs.items():
        relative = Path(name)
        shared.require(not relative.is_absolute() and relative.as_posix() == name and ".." not in relative.parts,
            "final_saved_artifact_path")
        path = folder/relative
        shared.require(path.resolve().is_relative_to(folder.resolve()) and path.resolve() == path.absolute(),
            "final_saved_artifact_escape")
        data = read_raw(path)
        shared.require(sha256(data).hexdigest() == identity, "final_external_artifact_ref_changed")
        artifacts[identity] = data
    with saved_only():
        checked = verify_final_mission(raw, expected_record_sha256=receipt["record_sha256"],
            expected_inputs=planning.verification_inputs(plan), artifacts=artifacts,
            host_records=host_raw, expected_host_sha256=receipt["host_sha256"], artifact_refs=refs)
    return checked


def require_fixture_sensor_readiness(plan, verified):
    """Require exercised fixture work before paid live sends, not flight success.

    Only call on the result returned by verify_saved, never a proof's claimed
    readiness or producer work flags. This does not change historical passed,
    issue execution permission, or prescribe a live model's action.
    """
    require = shared.require
    require(type(plan) is dict and plan.get("mode") == "fixture"
        and plan.get("case_id") in ("N", "T", "A") and type(verified) is dict
        and verified.get("schema") == "missionos.starship_final_native_verification.v3"
        and verified.get("passed") is True and verified.get("issues") == []
        and verified.get("mode") == "fixture" and verified.get("case_id") == plan["case_id"]
        and verified.get("plan_sha256") == plan["plan_sha256"]
        and verified.get("source_sha256") == shared.digest(plan["source_sha256"])
        and verified.get("context") == plan["context"]
        and verified.get("outcome", {}).get("all_required_physical_gates_met") is True,
        "final_fixture_sensor_readiness_binding")
    sensor, work = verified.get("checks", {}).get("sensor"), verified.get("work")
    require(type(sensor) is dict and type(work) is dict,
        "final_fixture_sensor_readiness_missing_work")
    actions, application = sensor.get("accepted_actions"), sensor.get("application")
    guard_count, fixture_count = sensor.get("independent_guard_receipts_recomputed"), sensor.get("fixture_judgments")
    require(type(actions) is list and actions
        and all(type(action) is str and action in ("continue_nominal_assumption",
            "request_independent_tracking", "adopt_accelerometer_estimate") for action in actions)
        and type(sensor.get("native_judgments")) is int and sensor["native_judgments"] == 0
        and type(fixture_count) is int and fixture_count >= 1
        and type(guard_count) is int and guard_count >= 0
        and type(sensor.get("fallback_count")) is int and sensor["fallback_count"] == 0
        and sensor.get("declined_work") is False
        and work.get("sensor_fallback_count") == 0
        and type(sensor.get("tracking_requested_by_accepted_judgment")) is bool
        and work.get("sensor_tracking_requested") is sensor["tracking_requested_by_accepted_judgment"]
        and work.get("sensor_once_only_application") is (application is not None),
        "final_fixture_sensor_pipeline_not_ready")
    if plan["case_id"] == "N":
        require(actions == ["continue_nominal_assumption"] and fixture_count == 1
            and sensor["tracking_requested_by_accepted_judgment"] is False
            and guard_count == 0 and application is None,
            "final_fixture_sensor_normal_work_changed")
    else:
        selected = "adopt_accelerometer_estimate" if plan["case_id"] == "T" else "continue_nominal_assumption"
        require(actions[0] == "request_independent_tracking"
            and actions.count("request_independent_tracking") == 1
            and len(actions) >= 2 and actions[-1] == selected
            and fixture_count >= 2 and sensor["tracking_requested_by_accepted_judgment"] is True
            and guard_count >= 1 and type(application) is dict
            and application.get("mode") == "fixture" and application.get("action") == selected
            and application.get("control_model_changed") is (plan["case_id"] == "T"),
            "final_fixture_sensor_diagnosis_pipeline_not_ready")
    return {"case_id": plan["case_id"], "fixture_sensor_pipeline_ready": True,
        "execution_authority_granted": False, "native_model_work_established": False}


def verify_prerequisites(plan):
    """Read external pins again; an embedded passed flag alone is not enough."""
    proof = plan["prerequisites"]
    shared.require(len(proof["artifacts"]) == 1, "final_one_external_prerequisite_manifest_required")
    pin = proof["artifacts"][0]
    raw = read_raw(shared.relative(pin["path"]))
    shared.require(sha256(raw).hexdigest() == pin["sha256"], "final_prerequisite_pin_changed")
    row = shared.parse(raw)
    shared.require(type(row) is dict and row.get("source_sha256") == plan["source_sha256"]
        and row.get("backend") == plan["backend"]
        and row.get("configuration_sha256") == shared.digest(plan["configuration"]), "final_prerequisite_current_binding")
    if plan["mode"] == "fixture":
        shared.require(row.get("schema") == "missionos.starship_final_boundary_preflight.v1"
            and row.get("provider_calls") == 0 and row.get("physics_steps") == 0
            and type(row.get("evidence")) is list and len(row["evidence"]) >= 3,
            "final_boundary_preflight_required")
        for item in row["evidence"]:
            data = read_raw(shared.relative(item["path"]))
            shared.require(sha256(data).hexdigest() == item["sha256"], "final_preflight_receipt_changed")
            value = shared.parse(data)
            shared.require(value.get("passed") is True or value.get("pytest_returncode") == 0,
                "final_boundary_preflight_not_passed")
        # A preflight is not current-physics recovery evidence. These fixture
        # runs are the first full feasibility checks, before any live cohort.
        return
    shared.require(row.get("schema") == "missionos.starship_final_fixture_cohort_proof.v1"
        and type(row.get("cases")) is list and {item["case_id"] for item in row["cases"]} == {"N", "T", "A"}
        and len(row["cases"]) == 3, "final_complete_fixture_cohort_required")
    for item in row["cases"]:
        fixture_plan = shared.read(shared.relative(item["plan_path"]))
        shared.require(fixture_plan["plan_sha256"] == item["plan_sha256"] and fixture_plan["mode"] == "fixture"
            and fixture_plan["case_id"] == item["case_id"]
            and fixture_plan["source_sha256"] == plan["source_sha256"] and fixture_plan["backend"] == plan["backend"]
            and fixture_plan["configuration"] == plan["configuration"], "final_fixture_counterpart_changed")
        result = verify_saved(fixture_plan, shared.relative(item["folder"]))
        shared.require(result.get("passed") is True and result.get("outcome", {}).get("all_required_physical_gates_met") is True,
            "final_fixture_cohort_not_independently_accepted")
        require_fixture_sensor_readiness(fixture_plan, result)


def worker(plan_path, folder, fds):
    from src.runtime.starship_final_outer import TOKEN, worker_channels
    from src.runtime.starship_final_native import run_final_native_worker
    from src.runtime.starship_restored_outer import offline_worker
    from src.runtime.starship_restored_environment import worker_environment_view
    plan = planning.validate_plan(shared.read(plan_path), current=True)
    shared.require(os.environ.get(TOKEN) == plan["plan_sha256"], "final_worker_exact_plan_opt_in")
    expected = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", TOKEN, "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"}
    shared.require(set(worker_environment_view()) <= expected, "final_worker_credentials_or_unexpected_environment")
    claim = shared.read(folder/"started.json")
    shared.require(claim == {"schema": "missionos.starship_final_run_claim.v1", "plan_sha256": plan["plan_sha256"],
        "run_id": plan["run_id"], "case_id": plan["case_id"], "source_commit": plan["source_commit"],
        "mode": plan["mode"], "automatic_retry": False}, "final_worker_run_claim")
    shared.save(folder/"worker.claim.json", claim)
    with offline_worker(), worker_channels(fds) as channels:
        record = run_final_native_worker(plan, output_dir=folder, channels=channels)
    save_worker_record(record, folder, plan_sha256=plan["plan_sha256"])


def save_worker_record(record, folder, *, plan_sha256):
    """Persist exact saved evidence without duplicating numerical trajectories."""
    from src.runtime.starship_final_record_storage import encode_record
    folder = Path(folder)
    raw = shared.wire(encode_record(record, folder))
    shared.require(len(raw) <= MAXIMUM_RECORD_BYTES, "final_record_write_budget")
    with (folder/"record.json.gz").open("xb") as stream:
        stream.write(gzip.compress(raw, mtime=0))
    shared.require(restore_record(folder/"record.json.gz") == raw, "final_worker_record_restore_mismatch")
    shared.save(folder/"worker-result.json", {"plan_sha256": plan_sha256, "record_sha256": sha256(raw).hexdigest(),
        "gzip_sha256": sha256(read_raw(folder/"record.json.gz")).hexdigest()})


def execute(plan_path, approved_hash, output_name, *, cohort_budget=None):
    from src.runtime.starship_final_outer import run_final_child
    from src.runtime.starship_final_gateway import loopback_gateway
    from src.runtime.starship_final_fixture import visual_scripts
    from src.intelligence.starship_visual_transport import fixture_provider
    from src.intelligence.starship_m2_broker import require_live_configuration
    from src.intelligence.starship_final_supervision_agent import LIVE_OPT_IN_ENV
    from src.intelligence.starship_visual_transport import LIVE_OPT_IN_ENV as VISUAL_ENV
    from contextlib import nullcontext
    plan = planning.validate_plan(shared.read(plan_path), current=True)
    shared.require(approved_hash == plan["plan_sha256"], "final_concrete_plan_execution_opt_in_required")
    verify_prerequisites(plan)
    folder = output_path(output_name)
    shared.require(folder.name == plan["run_id"], "final_run_directory_must_equal_unique_run_id")
    folder.mkdir(exist_ok=False)
    shared.save(folder/"plan.json", plan)
    claim = {"schema": "missionos.starship_final_run_claim.v1", "plan_sha256": plan["plan_sha256"],
        "run_id": plan["run_id"], "case_id": plan["case_id"], "source_commit": plan["source_commit"],
        "mode": plan["mode"], "automatic_retry": False}
    shared.save(folder/"started.json", claim)
    if plan["mode"] == "live":
        require_live_configuration({"mode": "live"})
        shared.require(os.environ.get(LIVE_OPT_IN_ENV) == os.environ.get(VISUAL_ENV) == "1",
            "final_native_and_visual_explicit_live_opt_in")
    with loopback_gateway(folder/"gateway") as (service, address), \
            (fixture_provider(visual_scripts()) if plan["mode"] == "fixture" else nullcontext(None)) as fixture:
        outer = run_final_child(plan, folder=folder, service=service, address=address,
            cohort_budget=cohort_budget,
            fixture_visual_endpoint=None if fixture is None else fixture["endpoint"],
            worker_command=lambda fds: [sys.executable, str(ROOT/SCRIPT), "_worker", "--plan", str(plan_path),
                "--folder", str(folder), "--fds", json.dumps(fds)])
    result = shared.read(folder/"worker-result.json")
    raw = restore_record(folder/"record.json.gz")
    shared.require(result == {"plan_sha256": plan["plan_sha256"], "record_sha256": sha256(raw).hexdigest(),
        "gzip_sha256": sha256(read_raw(folder/"record.json.gz")).hexdigest()}, "final_outer_worker_result_changed")
    paths = [folder/"outer-process.json", folder/"prefix.json.gz", folder/"outer-timing.json", folder/"worker-timing.json"]
    paths.extend(path for path in folder.glob("numerical-*/*/*") if path.name in (
        "job.json", "result.json.gz", "worker.log", "process-ready.json", "parent-observed.json"))
    paths.extend((folder/"camera"/"frames").glob("*.png"))
    refs = {str(path.relative_to(folder)): sha256(read_raw(path)).hexdigest() for path in paths}
    shared.save(folder/"outer-receipt.json", {"schema": "missionos.starship_final_outer_receipt.v1",
        "plan_sha256": plan["plan_sha256"], **outer, **result, "artifact_refs": refs})
    checked = verify_saved(plan, folder)
    shared.save(folder/"verification.json", checked)
    shared.require(checked.get("passed") is True, "final_saved_verification_failed_stop_no_retry")
    return checked


def run_cohort(cohort_path, approved_hash, output_name):
    """One frozen three-case attempt, with a single global clock/disk budget."""
    cohort = shared.read(cohort_path)
    shared.require(type(cohort) is dict and set(cohort) == {"schema", "plans", "cohort_sha256"}
        and cohort["schema"] == "missionos.starship_final_cohort_plan.v1"
        and cohort["cohort_sha256"] == approved_hash
        and approved_hash == shared.digest({k: v for k, v in cohort.items() if k != "cohort_sha256"})
        and type(cohort["plans"]) is list and len(cohort["plans"]) == 3, "final_concrete_cohort_opt_in")
    plans = [planning.validate_plan(value, current=True) for value in cohort["plans"]]
    shared.require([plan["case_id"] for plan in plans] == ["N", "T", "A"]
        and len({plan["run_id"] for plan in plans}) == 3
        and all(plan["mode"] == plans[0]["mode"] and plan["configuration"] == plans[0]["configuration"]
            and plan["source_sha256"] == plans[0]["source_sha256"] and plan["backend"] == plans[0]["backend"] for plan in plans),
        "final_matched_unique_cohort_conditions_required")
    for plan in plans:
        verify_prerequisites(plan)
    output = output_path(output_name)
    output.mkdir(exist_ok=False)
    shared.save(output/"started.json", {"cohort_sha256": approved_hash, "automatic_retry": False})
    from src.runtime.starship_final_timing import MissionWallBudget
    outcomes = []
    limits = plans[0]["configuration"]["bounds"]
    timing = MissionWallBudget(limits["cohort_wall_s"], clock=time.monotonic,
        reason="final_cohort_wall_budget_stop")

    def budgets():
        timing.check()
        shared.require(shutil.disk_usage(output).free >= limits["minimum_free_bytes"]
            and sum(path.stat().st_size for path in output.rglob("*") if path.is_file()) <= limits["maximum_output_bytes"],
            "final_cohort_disk_budget_stop")

    try:
        for plan in plans:
            budgets()
            plan_file = output/(plan["run_id"]+"-plan.json")
            shared.save(plan_file, plan)
            result = execute(plan_file, plan["plan_sha256"], output_name+"/"+plan["run_id"], cohort_budget=budgets)
            outcomes.append({"case_id": plan["case_id"], "run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
                "verification": result})
            shared.save(output/(plan["case_id"]+"-result.json"), outcomes[-1])
            if plan["mode"] == "fixture":
                shared.require(result["outcome"]["all_required_physical_gates_met"] is True,
                    "final_current_physics_fixture_gate_failed_stop")
        timing.finish()
        summary = {"schema": "missionos.starship_final_cohort_result.v1", "cohort_sha256": approved_hash,
            "cases": outcomes, "complete": True, "automatic_retry": False}
        shared.save(output/"result.json", summary)
        return summary
    except BaseException as error:
        shared.save(output/"stopped.json", {"schema": "missionos.starship_final_cohort_stopped.v1",
            "cohort_sha256": approved_hash, "completed_cases": [row["case_id"] for row in outcomes],
            "reason_type": type(error).__name__, "reason_code": str(error)[:160], "automatic_retry": False})
        raise
    finally:
        shared.save(output/"cohort-timing.json", {"cohort_sha256": approved_hash, "timing": timing.record()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="Freeze data only; no physics or provider calls")
    register.add_argument("--run-id", required=True)
    register.add_argument("--case", choices=("N", "T", "A"), required=True)
    register.add_argument("--mode", choices=("fixture", "live"), required=True)
    register.add_argument("--prerequisite", required=True)
    register.add_argument("--output", required=True)
    cohort = sub.add_parser("cohort", help="Execute one frozen, exact-hash three-case plan")
    cohort.add_argument("--plan", type=Path, required=True)
    cohort.add_argument("--approved-plan-sha256", required=True)
    cohort.add_argument("--output", required=True)
    for name in ("validate", "verify", "execute", "_worker"):
        child = sub.add_parser(name)
        child.add_argument("--plan", required=True, type=Path)
        if name == "execute":
            child.add_argument("--approved-plan-sha256", required=True)
            child.add_argument("--output", required=True)
        elif name in ("verify", "_worker"):
            child.add_argument("--folder", required=True, type=Path)
            if name == "_worker":
                child.add_argument("--fds", required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        config = planning.configuration(shared.read(shared.relative(planning.CONFIG)))
        pin_path = shared.relative(args.prerequisite)
        pins = {"kind": "fixed_source_boundary_preflight" if args.mode == "fixture" else "accepted_fixture_cohort",
            "same_configuration_sha256": shared.digest(config),
            "artifacts": [{"path": args.prerequisite, "sha256": sha256(read_raw(pin_path)).hexdigest()}]}
        value = planning.register(run_id=args.run_id, case_id=args.case, mode=args.mode,
            prerequisite_pins=pins, extra_sources=(SCRIPT,))
        shared.save(output_path(args.output), value)
        print(json.dumps({"plan_sha256": value["plan_sha256"], "execution_authority_granted": False}, sort_keys=True))
    elif args.command == "validate":
        plan = planning.validate_plan(shared.read(args.plan), current=True)
        verify_prerequisites(plan)
        print(json.dumps({"plan_sha256": plan["plan_sha256"], "validated": True, "physics_steps": 0, "provider_calls": 0}))
    elif args.command == "verify":
        print(json.dumps(verify_saved(shared.read(args.plan), args.folder), sort_keys=True))
    elif args.command == "_worker":
        worker(args.plan, args.folder, json.loads(args.fds))
    elif args.command == "cohort":
        print(json.dumps(run_cohort(args.plan, args.approved_plan_sha256, args.output), sort_keys=True))
    else:
        print(json.dumps(execute(args.plan, args.approved_plan_sha256, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
