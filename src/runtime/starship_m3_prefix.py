"""Opt-in fixture composition of concurrent launch, ascent and deployment.

This stops at the existing M2 checkpoint. It neither forecasts nor executes
Ship return. The separated booster's existing numerical branch still runs.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import re
from pathlib import Path
import sys
import time

from .starship_operational_trials import REGISTERED_CASES
from .starship_artifacts import write_verified_input
from .starship_ascent_supervisor import AscentSupervisor, CONFIG as ASCENT_CONFIG
from .starship_deployment_forecast import ObservedDeploymentDirector
from .starship_deployment_handoff import DeploymentTracker
from .starship_m2_contract import CHECKPOINT_TIME_S, m2_contract
from .starship_replanning import ROOT, digest, source_hashes
from .starship_return_sites import ReturnSites
from .starship_sixdof_mission import simulate_concurrent

CASES = ("normal", "operations_notice", "release_fault")
SCOPE = "concurrent_launch_ascent_deployment_prefix_only"
EXTRA_SOURCES = (
    "src/runtime/starship_concurrent.py",
    "src/runtime/starship_m3_directors.py",
    "src/runtime/starship_mission_director.py",
    "src/runtime/starship_splashdown.py",
    "src/runtime/starship_splashdown_verifier.py",
    "examples/spaceflight/starship-splashdown-goal.json",
    "examples/spaceflight/starship-return-sites-model-test.json",
    "src/runtime/starship_ascent_supervisor.py",
    "src/runtime/starship_m3_prefix.py",
    "src/runtime/starship_m3_prefix_verifier.py",
    "src/runtime/starship_deployment_forecast.py",
    "src/runtime/starship_deployment_handoff.py",
    "src/runtime/starship_deployment_handoff_verifier.py",
)


def m3_prefix_contract(case, *, mode="fixture", splashdown=True, legacy_call_limits=False, authority_gap_reasoning=None, legacy_booster_jev=False, recorded_booster_report=None):
    if (mode not in ("fixture", "live") or case not in (*CASES, "orbital_update", *REGISTERED_CASES)
            or type(splashdown) is not bool or mode == "live" and not splashdown):
        raise ValueError("m3_prefix_case_not_registered")
    inner = m2_contract(mode, case, legacy_call_limits=legacy_call_limits,
                        authority_gap_reasoning=authority_gap_reasoning)
    ascent = deepcopy(ASCENT_CONFIG)
    result = {
        "schema": "missionos.starship_m3_prefix_authority.v2" if splashdown else "missionos.starship_m3_prefix_authority.v1",
        "scope": SCOPE, "mode": mode, "case": case,
        "m2_authority": inner, "m2_authority_sha256": digest(inner),
        "ascent_authority": ascent, "ascent_authority_sha256": digest(ascent),
        "profile_sha256": inner["profile_sha256"], "checkpoint_time_s": CHECKPOINT_TIME_S,
        "booster_control": "fixed_v1", "booster_coast_control": "fixed_v1",
        "maximum_model_calls": 0, "maximum_forecasts": 0,
        "ship_return_dispatch_authorized": False,
        "physical_execution_authorized": False,
        "subauthority_policy": "outer_prefix_scope_intersects_embedded_m2_grant",
    }
    if splashdown:
        from .starship_mission_director import contract
        booster = contract(mode, splashdown=True)
        result.update(booster_authority=booster, booster_authority_sha256=digest(booster),
                      director_routing="body_specific_grants_v1")
    if mode == "live":
        result.update(schema="missionos.starship_m3_prefix_authority.v3",
            maximum_ascent_jev_calls=4, maximum_booster_jev_calls=1,
            prefix_provider_policy="jev_only",
            maximum_model_calls=inner["deployment_authority"]["maximum_jev_calls"]+5)
    if mode == "live" and not legacy_booster_jev:
        from src.intelligence.starship_booster_recovery_evidence import mission_recovery_report
        from .starship_recovery_reports import report_sources
        report = mission_recovery_report() if recorded_booster_report is None else deepcopy(recorded_booster_report)
        provenance = report_sources(report)
        result.update(schema="missionos.starship_m3_prefix_authority.v4",
            maximum_booster_jev_calls=0, maximum_booster_llm_calls=None,
            prefix_provider_policy="jev_ascent_deepseek_booster_report_v1",
            booster_recovery_report=report, booster_recovery_report_sha256=digest(report),
            booster_recovery_report_source_sha256=provenance,
            maximum_model_calls=None)
    return result


def sources():
    paths = set(source_hashes()) | set(EXTRA_SOURCES)
    result = {}
    for path in paths:
        module = sys.modules.get(path.removesuffix(".py").replace("/", ".")) if path.endswith(".py") else None
        active_path = Path(module.__file__) if module is not None and module.__file__ else ROOT/path
        result[path] = sha256(active_path.read_bytes()).hexdigest()
    return result


def _wait_live_mailboxes(mailbox, approved, run_id, *, clock=time.monotonic, sleep=time.sleep):
    """Allow host broker startup after worker spawn, before any physics tick."""
    deadline = clock()+5.
    phases = {"ascent", "booster", "deployment"}
    while phases:
        for phase in sorted(phases):
            try:
                ledger = json.loads((mailbox/phase/"host-attempts.json").read_text())
            except FileNotFoundError:
                continue
            expected_hash = digest(approved) if phase != "deployment" else approved["m2_authority_sha256"]
            schema = "missionos.starship_m3_host_attempts.v1" if phase != "deployment" else "missionos.starship_m2_host_attempts.v1"
            if (type(ledger) is not dict or ledger.get("run_id") != run_id or ledger.get("phase") != phase
                    or ledger.get("authority_sha256") != expected_hash or ledger.get("schema") != schema):
                raise ValueError("m3_live_mailbox_authority_mismatch")
            phases.remove(phase)
        if phases:
            if clock() >= deadline:
                raise ValueError("m3_live_host_ledger_startup_timeout")
            sleep(min(.01, deadline-clock()))


def run_prefix(profile, case, output, *, mode="fixture", mailbox=None, splashdown=True, run_id="m3-prefix-fixture",
               composition=None, visual_runtime=None):
    """One real prefix; caller owns its numerical-run budget and authorization."""
    if type(run_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
        raise ValueError("m3_prefix_run_id_invalid")
    approved, output = m3_prefix_contract(case, mode=mode, splashdown=splashdown), Path(output)
    if composition is not None:
        from .starship_unified_contract import validate_composition
        composition = validate_composition(composition)
        if (mode != "fixture" or not splashdown or case != "orbital_update"
                or approved != composition["authority"]["prefix_authority"]
                or visual_runtime is None or visual_runtime.composition != composition
                or visual_runtime.authority != composition["visual_authority"]):
            raise ValueError("unified_prefix_composition_binding")
    elif visual_runtime is not None:
        raise ValueError("m3_visual_requires_explicit_composition")
    if mode == "live":
        mailbox = Path(mailbox) if mailbox is not None else None
        if mailbox is None or mailbox.is_symlink() or any(not (mailbox/p).is_dir() or (mailbox/p).is_symlink() for p in ("ascent", "booster", "deployment", "return")):
            raise ValueError("m3_live_phase_mailboxes_required")
        _wait_live_mailboxes(mailbox, approved, run_id)
    if digest(profile) != approved["profile_sha256"]:
        raise ValueError("m3_prefix_requires_registered_profile")
    # This entry point owns its nested evidence directory. Composition callers
    # provide a fresh path; standalone callers may supply an empty directory.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("m3_prefix_fresh_output_required")
    frozen = sources()
    binding = {"schema": "missionos.starship_m3_prefix_approval_binding.v1",
               "mode": mode, "outer_authority_sha256": digest(approved),
               "m2_authority_sha256": approved["m2_authority_sha256"],
               "ascent_authority_sha256": approved["ascent_authority_sha256"],
               "human_approval_authenticated": False}
    if splashdown:
        binding["booster_authority_sha256"] = approved["booster_authority_sha256"]
        binding["run_id"] = run_id
    write_verified_input(output/"authority.json", approved)
    write_verified_input(output/"approval-binding.json", binding)
    write_verified_input(output/"source-sha256.json", frozen)
    tracker = DeploymentTracker(profile["payload"]["count"], approved["m2_authority_sha256"])
    from src.intelligence.starship_mission_director import fixture_decision
    decider = fixture_decision if mode == "fixture" else None
    director = ObservedDeploymentDirector(approved["m2_authority"]["deployment_authority"], tracker,
                                         fixture_decider=decider, run_id="m3-prefix-fixture")
    goal = None
    if splashdown:
        from .starship_m3_directors import M3Directors
        from .starship_splashdown import SplashdownGoal
        director = M3Directors(approved["m2_authority"]["deployment_authority"],
            approved["booster_authority"], tracker, fixture_decider=decider, run_id=run_id,
            mode=mode, mailbox=mailbox, visual_runtime=visual_runtime)
        goal = SplashdownGoal.from_dict(approved["booster_authority"]["splashdown_goal"])
    ascent = AscentSupervisor(approved["ascent_authority"], mode=mode,
        run_id=run_id if mode == "live" else "m3-prefix-fixture",
        mailbox=mailbox / "ascent" if mode == "live" else None)
    witnesses, sequence = [], 0

    def on_tick(tick):
        nonlocal sequence
        before = (ascent.request_count, len(ascent.records), len(ascent.confirmations), len(ascent.events),
                  sum(r.get("later_observation") is not None for r in ascent.records))
        ascent.observe(tick)
        after = (ascent.request_count, len(ascent.records), len(ascent.confirmations), len(ascent.events),
                  sum(r.get("later_observation") is not None for r in ascent.records))
        if tick.body_id == "ship" and before != after:
            witnesses.append({"sequence": sequence, "state": asdict(tick.state),
                              "observation": deepcopy(ascent.previous)})
        sequence += 1

    sites = ReturnSites.from_dict(json.loads((ROOT/"examples/spaceflight/starship-return-sites-model-test.json").read_text()),
        profile=profile, catch_config=json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text()))
    try:
        run = simulate_concurrent(profile, scenario="launch", duration_s=CHECKPOINT_TIME_S,
            mission_director=director, mission_case=approved["m2_authority"]["deployment_case"], return_sites=sites, splashdown_goal=goal,
            booster_policy="fixed_v1", development_booster_coast_control="fixed_v1",
            on_tick=on_tick, deployment_permission=ascent.deployment_permission,
            visual_runtime=visual_runtime,
            trouble_manifest=composition["prefix_faults"] if composition is not None else None)
    except Exception as error:
        if hasattr(error, "concurrent_execution"):
            write_verified_input(output/"partial-shared-clock.json", error.concurrent_execution)
        raise
    finally:
        trace = write_verified_input(output/"observation-trace.json", director.trace)
        supervision = write_verified_input(output/"ascent.json", ascent.finish())
        witnesses = write_verified_input(output/"ascent-witnesses.json", witnesses)
    if splashdown:
        run["booster_mission_director"] = director.finish_booster()
    run = write_verified_input(output/"prefix-run.json", run)
    handoff, handoff_error = None, None
    try:
        if run["outcome"]["phase"] != "orbital_coast" or director.pending.get("ship"):
            raise ValueError("m3_prefix_deployment_not_ready")
        handoff = tracker.freeze(run["final_state"], digest(profile))
        tracker.require_current(handoff)
        if composition is not None:
            from .starship_visual_verifier import validate_visual_origin
            validate_visual_origin(composition["visual_authority"], approved["m2_authority"],
                                   handoff, run, trace=trace)
        handoff = write_verified_input(output/"handoff.json", handoff)
    except ValueError as error:
        handoff = None
        handoff_error = str(error)  # Keep the actual prefix when no continuation is proved.
    if sources() != frozen:
        raise ValueError("m3_prefix_sources_changed_during_run")
    live_fields, model_calls = {}, 0
    if mode == "live":
        extra_ledger = {phase: json.loads((mailbox/phase/"host-attempts.json").read_text())
                        for phase in ("ascent", "booster")}
        deployment_ledger = json.loads((mailbox/"deployment"/"host-attempts.json").read_text())
        model_calls = sum(len(ledger["attempts"]) for ledger in (*extra_ledger.values(), deployment_ledger))
        live_fields = {"provider_attempt_ledger": extra_ledger,
                       "deployment_attempt_ledger": deployment_ledger}
    study = write_verified_input(output/"study.json", {
        "schema": "missionos.starship_m3_prefix_study.v1", "scope": SCOPE,
        **({"run_id": run_id} if splashdown else {}),
        "authority": approved, "approval_binding": binding, "profile": profile,
        **({"unified_composition": composition} if composition is not None else {}),
        "prefix_run": run, "ascent": supervision, "ascent_witnesses": witnesses,
        "trace": trace, "handoff": handoff, "handoff_error": handoff_error,
        "source_sha256": frozen, "model_calls": model_calls, "forecasts": 0, **live_fields,
        "ship_return_dispatched": False, "mission_completed": False,
        "m3_completed": False, "physical_execution": False})
    from .starship_m3_prefix_verifier import verify
    verification = write_verified_input(output/"verification.json", verify(study))
    write_verified_input(output/"summary.json", {
        "scope": SCOPE, "case": case, "verification_passed": verification["passed"],
        "handoff_ready": verification["handoff_ready"], "handoff_error": handoff_error,
        "outer_authority_sha256": digest(approved),
        "m2_authority_sha256": approved["m2_authority_sha256"],
        "ascent_decisions": len(supervision["decisions"]), "deployment_withheld": supervision["withheld"],
        "released_count": run["outcome"]["payload_released_count"],
        "ship_checkpoint_time_s": run["final_state"]["time_s"],
        "booster_checkpoint": run["booster_run"].get("shared_clock_checkpoint"),
        "booster_outcome": run["booster_run"]["outcome"],
        "booster_splashdown_verification": verification.get("booster_splashdown"),
        "model_calls": model_calls, "forecasts": 0, "ship_return_dispatched": False,
        "mission_completed": False, "m3_completed": False, "physical_execution": False})
    return study


def run_fixture(profile, case, output, *, splashdown=True, run_id="m3-prefix-fixture"):
    """Compatibility entry point with the original zero-provider authority."""
    return run_prefix(profile, case, output, splashdown=splashdown, run_id=run_id)
