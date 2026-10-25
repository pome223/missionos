"""Opt-in deployment and return from the same observed plant state.

Provider inference, when selected, lives in a separate host broker. A local CLI
opt-in alone does not demonstrate a Gateway grant or physical execution. Every attempted stage is saved; no failed flight is retried.
"""

from hashlib import sha256
import json
from pathlib import Path
import time

from .starship_operational_trials import is_trial
from .starship_artifacts import write_verified_checkpoint, write_verified_input
from .starship_deployment_forecast import ObservedDeploymentDirector
from .starship_deployment_handoff import DeploymentTracker
from .starship_m2_contract import m2_contract
from .starship_replanning import ROOT, digest, source_hashes as return_sources
from .starship_replanning_executor import ReplanningExecutor
from .starship_return_feasibility import readiness
from .starship_return_sites import ReturnSites
from .starship_sixdof_mission import simulate

EXTRA_SOURCES = (
    "src/runtime/starship_deployment_handoff.py",
    "src/runtime/starship_deployment_handoff_verifier.py",
    "src/runtime/starship_deployment_forecast.py",
    "src/runtime/starship_m2_contract.py",
    "src/runtime/starship_m2_runner.py",
    "src/runtime/starship_m2_verifier.py",
    "src/runtime/starship_return_time_search.py",
    "scripts/run_starship_m2.py",
    "src/runtime/starship_m2_control.py",
    "src/runtime/starship_m2_attempts.py",
    "src/runtime/starship_m2_decision_verifier.py",
    "src/intelligence/starship_m2_broker.py",
)


def sources():
    return {**return_sources(), **{name: sha256((ROOT / name).read_bytes()).hexdigest()
                                  for name in EXTRA_SOURCES}}


def budget(authority):
    returning = authority["return_authority"]
    return {
        "checkpoint_time_s": authority["checkpoint_time_s"],
        "maximum_model_calls": authority["maximum_model_calls"],
        "maximum_attempts": 1,
        **{
            key: returning[key]
            for key in (
                "maximum_forecasts",
                "maximum_workers",
                "forecast_timeout_s",
                "batch_timeout_s",
                "maximum_delay_s",
            )
        },
    }


def run_simulation(
    profile, case, output, *, mode="fixture", authority=None, mailbox=None, run_id="m2-fixture",
    concurrent_prefix=False, inflight_runtime=None, expected_prefix_authority=None,
    inflight_outer_authority=None, inflight_binding=None, visual_runtime=None
):
    """Run once and persist negative outcomes and partial failures alike."""
    if type(concurrent_prefix) is not bool:
        raise ValueError("m3_concurrent_prefix_flag")
    if concurrent_prefix and mode == "live" and ((case not in {"orbital_update", "release_fault"} and not is_trial(case)) or expected_prefix_authority is None):
        raise ValueError("m3_registered_live_outer_grant_required")
    m3_control = concurrent_prefix and (mode == "live" or case == "orbital_update" or is_trial(case))
    if expected_prefix_authority is not None and not concurrent_prefix:
        raise ValueError("m3_prefix_authority_requires_concurrent_fixture")
    if concurrent_prefix:
        from .starship_m3_prefix import m3_prefix_contract
        canonical_prefix = m3_prefix_contract(case, mode=mode)
        if expected_prefix_authority is None:
            expected_prefix_authority = canonical_prefix
        if digest(expected_prefix_authority) != digest(canonical_prefix):
            raise ValueError("m3_prefix_outer_authority_mismatch")
    executor_class, executor_options = ReplanningExecutor, {}
    if inflight_runtime is not None:
        # This is a trusted Python-runtime seam, never a CLI/JSON provider name.
        # The registered class alone receives bounded IPC handles, not operator
        # credentials or a host signing key.
        if (not concurrent_prefix
                or type(inflight_runtime) is not dict
                or set(inflight_runtime) != {"inflight", "request_writer", "grant_reader", "snapshot_sink", "clock"}):
            raise ValueError("m3_registered_inflight_runtime_required")
        if mode == "live":
            from .starship_m3_live import contract as outer_contract
            from .starship_m3_inflight import CASES, validate_binding
            outer_case = (inflight_outer_authority.get("case")
                          if type(inflight_outer_authority) is dict else None)
            if type(outer_case) is not str or outer_case not in CASES or CASES[outer_case] != case:
                raise ValueError("m3_live_inflight_outer_authority_required")
            expected_outer = outer_contract(mode, outer_case)
            if (inflight_outer_authority != expected_outer or authority != expected_outer["m2_authority"]
                    or expected_prefix_authority != expected_outer["prefix_authority"]):
                raise ValueError("m3_live_inflight_outer_authority_required")
            from .starship_m3_live import sources as outer_sources
            expected_contract = validate_binding(inflight_binding, expected_outer, run_id=run_id, sources=outer_sources())
            if inflight_runtime["inflight"] != expected_contract:
                raise ValueError("m3_live_inflight_runtime_binding")
        inflight = inflight_runtime["inflight"]
        if (type(inflight) is not dict
                or set(inflight) not in ({"schema", "context", "initial_scope", "bounds"},
                                        {"schema", "context", "initial_scope", "bounds", "fixture_policy"})
                or inflight["schema"] != "missionos.inflight_executor_contract.v1"
                or type(inflight["context"]) is not dict
                or set(inflight["context"]) != {"run_id", "mission_id", "vehicle_id"}
                or inflight["context"]["run_id"] != run_id+"-return"
                or not callable(inflight_runtime["snapshot_sink"])
                or not callable(inflight_runtime["clock"])):
            raise ValueError("m3_inflight_contract_context_mismatch")
        from .starship_inflight_executor import InflightReplanningExecutor
        executor_class, executor_options = InflightReplanningExecutor, dict(inflight_runtime)
    output = Path(output)
    if output.exists():
        raise ValueError("m2_fresh_output_required")
    from .starship_m2_control import validate_worker_input

    authority = m2_contract(mode, case) if authority is None else authority
    validate_worker_input(authority, mode=mode, case=case, run_id=run_id, mailbox=mailbox)
    if visual_runtime is not None:
        from .starship_visual_contract import validate_authority
        from .starship_visual_director import VisualRuntime
        if (type(visual_runtime) is not VisualRuntime or concurrent_prefix or mode != "fixture"
                or visual_runtime.run_id != run_id
                or validate_authority(visual_runtime.authority)["m2_authority"] != authority):
            raise ValueError("visual_runtime_authority_binding")
    if inflight_runtime is not None:
        from .starship_inflight_executor import validate_inflight_executor_contract
        validate_inflight_executor_contract(inflight_runtime["inflight"], authority["return_authority"],
                                            run_id=run_id+"-return")
    output.mkdir(parents=True, mode=0o700)
    source_provider = sources
    if visual_runtime is not None:
        from .starship_visual_contract import sources as source_provider
    if concurrent_prefix:
        from .starship_m3_fixture import sources as source_provider
        if m3_control:
            from .starship_m3_live import sources as source_provider
    if inflight_runtime is not None and not m3_control:
        from .starship_m3_fixture import sources as m3_sources
        def source_provider():
            return m3_sources(include_inflight=True)
    frozen_sources, inner_sources = source_provider(), return_sources()
    study = {
        "schema": "missionos.starship_m2_study.v1",
        "run_id": run_id,
        "authority": authority,
        "profile": profile,
        "source_sha256": frozen_sources,
        "return_source_sha256": inner_sources,
        "budget": budget(authority),
        "deployment_run": None,
        "trace": [],
        "handoff": None,
        "return_study": None,
        "runtime": {
            "schema": "missionos.starship_m2_runtime.v1",
            "mode": mode,
            "boundary": "local_cli_fixture_continuation"
            if mode == "fixture"
            else "host_broker_mailbox_continuation",
            "deployment_started": False,
            "continuation_started": False,
            "retry_performed": False,
            "model_calls": 0,
            "human_inflight_commands": 0,
            "physical_execution": False,
        },
    }
    if inflight_runtime is not None:
        study["inflight_contract"] = json.loads(json.dumps(inflight_runtime["inflight"], allow_nan=False))
        study["runtime"]["registered_inflight_executor"] = True
    started, actor, director, tracker = time.monotonic(), None, None, None
    write_verified_input(output / "inputs.json", {
        "authority": authority, "profile": profile, "run_id": run_id,
        "source_sha256": frozen_sources, "return_source_sha256": inner_sources,
        "budget": study["budget"],
        **({"inflight_contract": study["inflight_contract"]} if inflight_runtime is not None else {}),
    })
    for name in frozen_sources:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    try:
        _, _, error = readiness(profile)
        if error:
            raise ValueError(error)
        if concurrent_prefix:
            from .starship_m3_fixture import produce_prefix, validate_prefix
            if m3_control:
                from .starship_m3_live import produce_prefix, validate_prefix
            study["runtime"]["deployment_started"] = True
            prefix = produce_prefix(profile, authority, output / "concurrent-prefix",
                expected_prefix_authority=expected_prefix_authority, run_id=run_id,
                **({"mailbox": mailbox} if m3_control else {}))
            study["concurrent_prefix"] = prefix
            launch, handoff = prefix["prefix_run"], prefix["handoff"]
            study["trace"] = write_verified_input(output / "observation-trace.json", prefix["trace"])
            study["deployment_run"] = write_verified_input(output / "deployment-run.json", launch)
            study["handoff"] = write_verified_input(output / "handoff.json", handoff)
            validate_prefix(prefix, authority, profile, expected_prefix_authority=expected_prefix_authority, expected_run_id=run_id)
        else:
            tracker = DeploymentTracker(profile["payload"]["count"], digest(authority))
            from src.intelligence.starship_mission_director import fixture_decision

            director_class, director_kwargs = ObservedDeploymentDirector, {}
            if visual_runtime is not None:
                from .starship_visual_director import VisualDeploymentDirector
                director_class = VisualDeploymentDirector
                director_kwargs = {"visual_runtime": visual_runtime}
            director = director_class(
                authority["deployment_authority"],
                tracker,
                mailbox=Path(mailbox) / "deployment" if mode == "live" else None,
                fixture_decider=fixture_decision if mode == "fixture" else None,
                run_id=run_id + "-deployment",
                response_fault=authority["deployment_response_fault"],
                **director_kwargs,
            )
            sites = ReturnSites.from_dict(
                json.loads((ROOT / "examples/spaceflight/starship-return-sites-model-test.json").read_text()),
                profile=profile,
                catch_config=json.loads((ROOT / "examples/spaceflight/starship-catch-profile.json").read_text()),
            )
            study["runtime"]["deployment_started"] = True
            launch = simulate(profile, mission_director=director,
                              mission_case=authority["deployment_case"], return_sites=sites,
                              duration_s=authority["checkpoint_time_s"],
                              **({"visual_runtime": visual_runtime} if visual_runtime is not None else {}))
            if visual_runtime is not None:
                launch["visual_observation"] = visual_runtime.finish()
            launch = write_verified_input(output / "deployment-run.json", launch)
            study["deployment_run"] = launch
            if director.pending.get("ship"):
                raise ValueError("m2_unconfirmed_deployment_command")
            if visual_runtime is not None:
                visual_runtime.require_inventory_ready(launch["final_state"]["time_s"],
                                                       launch["outcome"]["payload_released_count"])
            handoff = tracker.freeze(launch["final_state"], digest(profile))
            handoff = write_verified_input(output / "handoff.json", handoff)
            study["handoff"] = handoff
            tracker.require_current(handoff)
        continuation_output = output / "return"
        continuation_output.mkdir()
        actor = executor_class(
            profile,
            authority["return_authority"],
            authority["return_case"],
            continuation_output,
            run_id=run_id + "-return",
            mailbox=Path(mailbox) / "return" if mode == "live" else None,
            deployment={"authority": authority, "handoff": handoff, "launch": launch,
                        **({"visual_authority": visual_runtime.authority} if visual_runtime is not None else {})},
            **executor_options,
        )
        study["runtime"]["continuation_started"] = True
        study["return_study"] = actor.run()
        if tracker is not None:
            tracker.require_current(handoff)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error), "retry_performed": False}
        study["runtime_failure"] = failure
        write_verified_input(output / "failure.json", failure)
        if actor is not None:
            study["return_study"] = actor.finish()
            study["return_study"]["runtime_failure"] = type(error).__name__ + ":" + str(error)
    finally:
        if director is not None:
            study["trace"] = write_verified_input(output / "observation-trace.json", director.trace)
        study["runtime"]["model_calls"] = sum(
            bool(((record.get("response") or {}).get(field) or {}).get("call_attempted"))
            for records in (
                study["deployment_run"]["mission_director"]["records"]
                if study["deployment_run"]
                else [],
                study["return_study"]["decisions"] if study["return_study"] else [],
            )
            for record in records
            for field in ("jev_invocation", "llm_invocation")
        )
        if mode == "live":
            from .starship_m2_attempts import snapshot
            study["runtime"]["actor_record_model_calls"] = study["runtime"]["model_calls"]
            try:
                study["provider_attempt_ledger"] = snapshot(mailbox, authority, run_id)
                study["runtime"]["model_calls"] = sum(len(row["attempts"])
                    for row in study["provider_attempt_ledger"].values())
            except (ValueError, OSError, TypeError, KeyError) as error:
                study["runtime_failure"] = {"type":type(error).__name__,
                    "message":"host_attempt_ledger_unavailable", "retry_performed":False}
        study["runtime"]["wall_time_s"] = time.monotonic() - started
        study["runtime"]["sources_unchanged"] = source_provider() == frozen_sources and return_sources() == inner_sources
        # The M3/visual outer study embeds these complete values. Preserve nested
        # checkpoints losslessly without storing another uncompressed copy.
        save_checkpoint = write_verified_checkpoint if concurrent_prefix or visual_runtime is not None else write_verified_input
        if study["return_study"] is not None:
            study["return_study"] = save_checkpoint(output / "return-study.json", study["return_study"])
        study = save_checkpoint(output / "study.json", study)
    return study


def run_fixture(profile, case, output, *, run_id="m2-fixture"):
    return run_simulation(profile, case, output, run_id=run_id)
