"""Opt-in M2 deployment observation -> return forecast boundary.

Only the existing fixture director commands the deployment plant. This adapter
records its observed separation counter and command transitions, then forecasts
from the resulting state. A forecast never resumes or replaces the live plant.
No model calls, Gateway grant, return dispatch, or M2 mission success is implied.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from .starship_artifacts import write_verified_input
from .starship_deployment_handoff import DeploymentTracker, bind_handoff
from .starship_mission_director import MissionDirector, contract, digest
from .starship_replanning import source_hashes as m1_sources
from .starship_replanning_executor import prediction_job
from .starship_return_feasibility import readiness
from .starship_return_sites import ReturnSites
from .starship_sixdof_mission import simulate

ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT_TIME_S = 1000.7000000000407
CASES = ("normal", "operations_notice", "release_fault", "hold_resume")
EXTRA_SOURCES = (
    "src/runtime/starship_deployment_handoff.py",
    "src/runtime/starship_deployment_handoff_verifier.py",
    "src/runtime/starship_deployment_forecast.py",
    "scripts/run_starship_m2_handoff.py",
)


def sources():
    return {**m1_sources(), **{p: sha256((ROOT / p).read_bytes()).hexdigest()
                              for p in EXTRA_SOURCES}}


def authority(case):
    if case not in CASES:
        raise ValueError("unknown_m2_handoff_fixture")
    return {
        "schema": "missionos.m2_handoff_fixture_authority.v1",
        "case": case,
        "deployment_authority": contract("fixture"),
        "checkpoint_time_s": CHECKPOINT_TIME_S,
        "maximum_forecasts": 1,
        "forecast_wall_timeout_s": 180.0,
        "prediction_integration_scale": 1.0,
        "prediction_variant": "quantized_navigation_center_only",
        "maximum_model_calls": 0,
        "ship_return_dispatch_authorized": False,
        "physical_execution_authorized": False,
    }


class ObservedDeploymentDirector(MissionDirector):
    """Capture the existing sensor boundary without changing physics/control."""

    def __init__(self, envelope, tracker, **kwargs):
        super().__init__(envelope, **kwargs)
        self.tracker, self.trace = tracker, []

    def confirm(self, clock_id, row):
        if clock_id == "ship":
            self.tracker.observe(row)
            self.trace.append({"kind": "observation", "row": deepcopy(row)})
        super().confirm(clock_id, row)

    def update(self, point, row, **kwargs):
        action = super().update(point, row, **kwargs)
        if action is not None and point.startswith("deployment"):
            # This executes before simulate applies the command. A resume
            # invalidates predictions even before the next separation occurs.
            self.tracker.command(action, row["time_s"])
            self.trace.append({"kind": "command", "action": action, "time_s": row["time_s"]})
        return action


def run_fixture(profile, case, output):
    """One bounded producer and one forecast; preserve every completed stage."""
    output = Path(output)
    approved = authority(case)
    frozen_sources = sources()
    _, _, error = readiness(profile)
    if error:
        raise ValueError(error)
    write_verified_input(output / "authority.json", approved)
    write_verified_input(output / "source-sha256.json", frozen_sources)
    tracker = DeploymentTracker(profile["payload"]["count"], digest(approved))
    from src.intelligence.starship_mission_director import fixture_decision
    director = ObservedDeploymentDirector(
        approved["deployment_authority"], tracker,
        fixture_decider=fixture_decision, run_id="m2-handoff-fixture",
        response_fault="hold_deployment_start" if case == "hold_resume" else None,
    )
    # simulate() also integrates the existing booster branch. Its Director
    # choices require the same model-test site definitions as the managed CLI.
    sites = ReturnSites.from_dict(
        json.loads((ROOT / "examples/spaceflight/starship-return-sites-model-test.json").read_text()),
        profile=profile,
        catch_config=json.loads((ROOT / "examples/spaceflight/starship-catch-profile.json").read_text()),
    )
    print(json.dumps({"stage": "deployment", "case": case}), flush=True)
    try:
        run = simulate(profile, mission_director=director,
                       mission_case="normal" if case == "hold_resume" else case,
                       return_sites=sites,
                       duration_s=approved["checkpoint_time_s"])
    finally:
        # Preserve the observed/command prefix even if the producer fails.
        trace = write_verified_input(output / "observation-trace.json", director.trace)
    run = write_verified_input(output / "deployment-run.json", run)
    if run["outcome"]["phase"] != "orbital_coast" or director.pending.get("ship"):
        raise ValueError("m2_deployment_not_ready_for_handoff")
    handoff = tracker.freeze(run["final_state"], digest(profile))
    handoff = write_verified_input(output / "handoff.json", handoff)
    tracker.require_current(handoff)
    cutoffs = [e["time_s"] for e in run["events"] if e["event"] == "orbit_cutoff_command"]
    if len(cutoffs) != 1:
        raise ValueError("unique_original_return_schedule_required")
    scheduled = cutoffs[0] + profile["guidance"]["coast_before_return_s"]
    print(json.dumps({"stage": "forecast", "retained_count": handoff["retained_count"],
                      "return_time_s": scheduled}), flush=True)
    prediction = prediction_job((handoff["origin"], profile, scheduled, scheduled,
                                 str(output / "forecast.json"), 1.0))
    tracker.require_current(handoff)
    if sources() != frozen_sources:
        raise ValueError("m2_sources_changed_during_run")
    prediction = bind_handoff(prediction, handoff)
    study = {
        "schema": "missionos.starship_m2_handoff_study.v1",
        "authority": approved,
        "profile": profile,
        "deployment_run": run,
        "trace": trace,
        "handoff": handoff,
        "prediction": prediction,
        "source_sha256": frozen_sources,
        "model_calls": 0,
        "ship_return_dispatched": False,
        "physical_execution": False,
        "mission_completed": False,
    }
    return write_verified_input(output / "study.json", study)
