"""Prepare, approve once, and run a separate-process experimental trouble flight.

This is a MissionOS CLI boundary, not a chat/Gateway route or M3 qualification.
The host alone invokes providers; the worker sees only bounded decisions.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_trouble_control import TroubleControl, authority, digest, record_terminal_interruptions  # noqa: E402
from src.runtime.starship_trouble_engine import FAMILIES, make_manifest, validate_manifest  # noqa: E402

PROFILE = "examples/spaceflight/starship-sixdof-profile.json"


def sources():
    paths = sorted((ROOT/"src/runtime").glob("starship_*.py"))
    paths += sorted((ROOT/"src/intelligence").glob("starship_*.py"))
    paths += [ROOT/"src/intelligence/space_ops_investigator.py", ROOT/"src/agents/model_config.py",
        ROOT/"src/config/settings.py", ROOT/PROFILE, Path(__file__).resolve()]
    return {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in paths}


def backend():
    return {"python": sys.version.split()[0], **{name: importlib.metadata.version(name) for name in ("numpy", "scipy")}}


def output_bytes(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def resources(root, grant):
    if output_bytes(root) > grant["maximum_output_bytes"]:
        raise ValueError("trouble_control_output_limit")
    if shutil.disk_usage(root).free < grant["minimum_free_bytes"]:
        raise ValueError("trouble_control_free_space_floor")


def load_plan(root, *, host=False):
    plan = json.loads((root/"plan.json").read_text())
    manifest = validate_manifest(json.loads((root/"manifest.json").read_text()))
    grant = plan["authority"]
    if (plan["schema"] != "missionos.starship_trouble_plan.v1" or plan["backend"] != backend()
            or grant != authority(plan["profile"], manifest, sources(), grant["duration_s"], grant["mode"], provider_models=grant["provider_models"])):
        raise ValueError("trouble_control_frozen_plan_mismatch")
    if host and grant["mode"] == "live":
        from src.intelligence.starship_trouble_agent import provider_models
        if provider_models() != grant["provider_models"]:
            raise ValueError("trouble_approved_provider_model_changed")
    resources(root, grant)
    return plan, manifest


def prepare(args):
    if args.output_dir.exists():
        raise ValueError("fresh_output_required_no_retry_or_overwrite")
    profile = json.loads((ROOT/PROFILE).read_text())
    manifest = (validate_manifest(json.loads(args.manifest.read_text())) if args.manifest else
        make_manifest(args.seed, allowed_families=args.family, max_events=args.max_events,
                      nominal_probability=args.nominal_probability))
    duration = args.duration_s if args.duration_s is not None else 800.
    models = None
    if args.mode == "live":
        from src.intelligence.starship_trouble_agent import provider_models
        models = provider_models()
    grant = authority(profile, manifest, sources(), duration, args.mode, provider_models=models)
    plan = {"schema": "missionos.starship_trouble_plan.v1", "profile": profile,
        "backend": backend(), "authority": grant,
        "scope": "Experimental current-observation fault response. No nominal certificate is applied to the faulted plant. Return, survival, catch and full M3 completion are not certified."}
    args.output_dir.mkdir(parents=True)
    resources(args.output_dir, grant)
    write_verified_input(args.output_dir/"manifest.json", manifest)
    write_verified_input(args.output_dir/"plan.json", plan)
    print(json.dumps({"plan_sha256": digest(plan), "mode": args.mode,
        "duration_s": duration, "actions": grant["actions"], "maximum_llm_calls": None,
        "fault_return_qualified": False, "approval_required": True}))
    return 0


def worker(root):
    from src.runtime.starship_sixdof_mission import simulate_concurrent
    from src.runtime.starship_sixdof_verifier import verify_study
    from src.runtime.starship_trouble_verifier import verify_run as verify_faults
    from src.runtime.starship_trouble_control_verifier import verify_run as verify_control
    plan, manifest = load_plan(root)
    grant = plan["authority"]
    approved = json.loads((root/"approval.json").read_text())
    if approved["plan_sha256"] != digest(plan) or not (root/"execution.claimed").is_file():
        raise ValueError("trouble_control_approval_required")
    if any(key in os.environ for key in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        raise ValueError("provider_credentials_in_worker")
    with (root/"worker.started").open("x") as handle:
        handle.write(digest(plan)+"\n")
    actor = TroubleControl(grant, plan["profile"], manifest, grant["source_sha256"], mailbox=root/"mailbox")
    started = time.monotonic()
    try:
        run = simulate_concurrent(plan["profile"], duration_s=grant["duration_s"],
            trouble_manifest=manifest, trouble_control=actor)
        record_terminal_interruptions(run)
        study = {"schema": "missionos.starship_sixdof_study.v1", "title": "STARSHIP / GOVERNED TROUBLE RESPONSE",
            "profile": plan["profile"], "runs": [run], "provenance": {
                "source_sha256": grant["source_sha256"], "backend": plan["backend"],
                "plan_sha256": digest(plan), "provider_credentials_in_worker": False,
                "physical_execution_invoked": False, "starship_vehicle_validated": False,
                "fault_return_qualified": False}}
        write_verified_input(root/"study.json", study)
        saved = json.loads((root/"study.json").read_text())
        sixdof = verify_study(saved, expected_scenario="launch")
        faults = verify_faults(saved["runs"][0])
        control = verify_control(saved["runs"][0], plan["profile"], grant)
        checks = {"passed": sixdof["passed"] and faults["passed"] and control["passed"],
            "control_completion_accepted": control["case_accepted"],
            "sixdof": sixdof, "faults": faults, "control": control}
        write_verified_input(root/"verification.json", checks)
        responses = [row["response"] for row in saved["runs"][0]["trouble_control"]["records"] if row["response"]]
        providers = {}
        for provider, key in (("jev", "jev_invocation"), ("deepseek", "llm_invocation")):
            receipts = [row[key] for row in responses if row.get(key)]
            providers[provider] = {"attempted": sum(x.get("call_attempted") is True for x in receipts),
                "valid": sum(x.get("status") == "succeeded" for x in receipts),
                "statuses": [x["status"] for x in receipts]}
        summary = {"schema": "missionos.starship_trouble_control_summary.v1", "record_checks_passed": checks["passed"],
            "control_completion_accepted": checks["control_completion_accepted"],
            "source_unchanged": sources() == grant["source_sha256"], "providers": providers,
            "accepted_live_decisions": control["accepted_live_decisions"],
            "actions": [{"body": row["observation"]["body"], "time_s": row["time_s"], "action": row["action"], "fallback": row["fallback"], "later_observation_s": row["later_observation"]["time_s"] if row["later_observation"] else None}
                for row in run["trouble_control"]["records"]],
            "outcome": run["outcome"], "booster_outcome": run["booster_run"]["outcome"] if run["booster_run"] else None,
            "applied_faults": len(run["trouble"]["receipts"]), "fault_return_qualified": False,
            "physical_execution": False, "wall_s": time.monotonic()-started,
            "study_sha256": sha256((root/"study.json").read_bytes()).hexdigest(),
            "claim": "Experimental mission decisions and later observations; record consistency is not successful recovery"}
        write_verified_input(root/"summary.json", summary)
        resources(root, grant)
        return 0 if checks["passed"] and checks["control_completion_accepted"] and summary["source_unchanged"] else 2
    except Exception as error:
        write_verified_input(root/"worker-failure.json", {"exception": type(error).__name__, "detail": str(error),
            "concurrent_execution": getattr(error, "concurrent_execution", None),
            "control": actor.finish(), "automatic_retry": False})
        raise


def execute(args):
    from src.intelligence.starship_m2_broker import require_live_configuration
    from src.intelligence.starship_trouble_agent import serve
    root = args.output_dir.resolve()
    plan, _ = load_plan(root, host=True)
    grant = plan["authority"]
    if args.approve_plan_sha256 != digest(plan):
        raise ValueError("approval_hash_mismatch")
    require_live_configuration(grant)
    # Exclusive claim consumes this exact approval even if the child fails.
    with (root/"execution.claimed").open("x") as handle:
        handle.write(digest(plan)+"\n")
    write_verified_input(root/"approval.json", {"plan_sha256": digest(plan), "authority_sha256": digest(grant),
        "operator": "explicit_cli_test_operator", "identity_authenticated": False,
        "one_use": True, "scope": "synthetic_simulation_only", "provider_mode": grant["mode"]})
    (root/"mailbox").mkdir()
    for directory in ("ship", "booster", "inputs", "responses", "attempts", "errors"):
        (root/"mailbox"/directory).mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    started = time.monotonic()
    child = None
    try:
        with (root/"worker.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", "--output-dir", str(root)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            def active():
                return child.poll() is None and time.monotonic()-started < grant["maximum_wall_time_s"]
            threads = serve(root/"mailbox", grant, child, active)
            next_source_check = started
            while child.poll() is None:
                resources(root, grant)
                if time.monotonic() >= next_source_check:
                    if sources() != grant["source_sha256"]:
                        raise ValueError("trouble_control_sources_changed")
                    next_source_check = time.monotonic()+1.
                # active() also reports normal child completion to brokers.
                # The child can finish during the source/resource checks; that
                # is not a wall deadline violation. Check elapsed time alone.
                if time.monotonic()-started >= grant["maximum_wall_time_s"]:
                    raise ValueError("trouble_control_wall_deadline")
                time.sleep(.1)
            for thread in threads:
                thread.join(timeout=5)
        unchanged = sources() == grant["source_sha256"]
        attempts = [json.loads(path.read_text()) for path in sorted((root/"mailbox/attempts").glob("*-started.json"))]
        finished = [json.loads(path.read_text()) for path in sorted((root/"mailbox/attempts").glob("*-finished.json"))]
        write_verified_input(root/"host-execution.json", {"child_exit_code": child.returncode,
            "source_unchanged": unchanged, "wall_s": time.monotonic()-started,
            "credential_free_worker": True, "no_automatic_retry": True,
            "provider_attempts": len(attempts), "provider_finished": len(finished),
            "provider_outcomes": [{"provider": row["provider"], "status": row["status"],
                "request_sha256": row["request_sha256"], "model_inference_invoked": row["model_inference_invoked"]} for row in finished],
            "scope": "All host attempts, including responses rejected as stale, distinct from accepted flight decisions"})
        print(json.dumps({"child_exit_code": child.returncode, "source_unchanged": unchanged}))
        return child.returncode if unchanged else 2
    except Exception as error:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        write_verified_input(root/"host-failure.json", {"exception": type(error).__name__, "detail": str(error), "automatic_retry": False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--prepare", action="store_true")
    operation.add_argument("--approve-plan-sha256")
    operation.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--seed", type=int)
    identity.add_argument("--manifest", type=Path)
    parser.add_argument("--family", action="append", choices=tuple(FAMILIES))
    parser.add_argument("--max-events", type=int, default=1)
    parser.add_argument("--nominal-probability", type=float, default=.2)
    parser.add_argument("--duration-s", type=float)
    args = parser.parse_args(argv)
    if args.prepare and args.seed is None and args.manifest is None:
        parser.error("prepare requires a seed or exact manifest")
    if args.manifest and (args.family is not None or args.max_events != 1 or args.nominal_probability != .2):
        parser.error("manifest already fixes the distribution")
    return prepare(args) if args.prepare else worker(args.output_dir.resolve()) if args.worker else execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
