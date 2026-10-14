"""Record corruption and runner wiring. No numerical integration or models."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.runtime import starship_m3_prefix as prefix
from src.runtime.starship_ascent_supervisor import CONFIG
from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_m3_prefix_verifier import verify
from src.runtime.starship_replanning import digest, estimate
from src.runtime.starship_sixdof import EngineState, State6DOF
from src.runtime.starship_sixdof_mission import vehicle


def canonical(value):
    return json.loads(json.dumps(value))


def state(t, *, healthy=True):
    return State6DOF(t, (6_578_137., 0., 0.), (0., 7800., 0.), (1., 0., 0., 0.),
                    (0., 0., 0.), 100_000., (EngineState(available=healthy),), (0.,))


def observed(tick, *, healthy=True):
    return {k: tick[k] for k in ("time_s", "phase", "sequencer_state", "released_count", "deployment_withheld")} | {
        "navigation": estimate(state(tick["time_s"], healthy=healthy)), "engine_available": [healthy]}


@pytest.fixture
def make_study():
    """Synthetic archived records; physics validity is intentionally not claimed."""
    def make(withheld=False, case="normal"):
        profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
        authority = prefix.m3_prefix_contract(case, splashdown=False)
        stop = prefix.CHECKPOINT_TIME_S
        # One-second ticks plus phase transitions, without executing any plant.
        times = [0., .1, .2, .3, *[float(t)*.25+.3 for t in range(1, 4003)]]
        times = [t for t in times if t < stop]
        rows = []
        for i, t in enumerate(times):
            phase = "stack_ascent" if t <= .1 else "ship_ascent" if t == .2 else "orbital_coast"
            count = 0 if withheld else min(26, max(0, int(t)))
            rows.append({"body_id": "ship", "time_s": t, "next_time_s": times[i+1] if i+1 < len(times) else stop,
                "phase": phase, "sequencer_state": "skipped" if withheld and t >= .2 else "running",
                "released_count": count, "deployment_withheld": withheld and t >= .2})
        rows += [{"body_id": "booster", "time_s": .2, "next_time_s": .3, "phase": "booster_return"}]
        rows.sort(key=lambda r: (r["time_s"], r["body_id"]))
        for i, row in enumerate(rows):
            row["sequence"] = i
        ship_rows = [r for r in rows if r["body_id"] == "ship"]
        request_observation = observed(ship_rows[0])
        decision_observation = observed(ship_rows[1])
        action = "withhold_deployment" if withheld else "continue_mission"
        request = {"schema": "missionos.starship_ascent_decision.v1", "request_id": "fixture-ascent-0",
            "authority_sha256": digest(CONFIG), "observation": request_observation,
            "trigger": "initial_monitor", "deadline_s": 12., "allowed_actions": CONFIG["actions"]}
        response = {"request_id": request["request_id"], "request_sha256": digest(request),
                    "action": action, "mode": "fixture", "model_inference_invoked": False}
        decision = {"request": request, "response": response, "accepted_action": action,
            "action": action, "fallback": False, "time_s": .1, "observation": decision_observation,
            "stale_health": False, "physical_engine_command": False}
        confirmation = observed(ship_rows[2])
        witness_rows = ship_rows[:3 if withheld else 2]
        witnesses = [{"sequence": row["sequence"], "state": asdict(state(row["time_s"])),
            "observation": observed(row)} for row in witness_rows]
        ascent = {"schema": "missionos.starship_ascent_supervision.v1", "authority": CONFIG, "mode": "fixture",
            "observations": [request_observation, decision_observation], "decisions": [decision],
            "trigger_events": [{"time_s": 0., "trigger": "initial_monitor", "request_id": request["request_id"]}],
            "requests": 1, "pending": None, "withheld": withheld,
            "confirmations": [{"command_time_s": .1, "later_observation": confirmation}] if withheld else [],
            "physical_execution": False, "abort_trajectory_implemented": False}
        events = [{"event": "stage_separation", "time_s": .2}]
        if withheld:
            events.insert(0, {"event": "supervised_deployment_withheld", "time_s": .1})
        trace, satellites = [], []
        tracker = DeploymentTracker(26, authority["m2_authority_sha256"])
        count = 0
        for row in ship_rows:
            if row["phase"] != "orbital_coast":
                continue
            observation = {"phase": "orbital_coast", "time_s": row["time_s"],
                "released_count": row["released_count"], "sequencer_state": row["sequencer_state"]}
            if row["released_count"] > count:
                event_time = trace[-1]["row"]["time_s"]
                events.append({"event": "payload_released", "time_s": event_time})
                satellites.append({"release_state": asdict(state(event_time))})
                count += 1
            tracker.observe(observation)
            trace.append({"kind": "observation", "row": observation})
        final = asdict(state(stop))
        handoff = tracker.freeze(final, digest(profile))
        run = {"scenario": "launch", "mission_management_case": case,
            "mission_director": {"envelope": authority["m2_authority"]["deployment_authority"], "response_fault": None, "records": []},
            "events": events, "samples": [{**final, "phase": "orbital_coast"}], "final_state": final,
            "final_vehicle": asdict(vehicle(profile, payload_count=26-count)),
            "outcome": {"phase": "orbital_coast", "payload_released_count": count}, "satellites": satellites,
            "booster_separation_state": asdict(state(.2)),
            "booster_run": {"booster_separation_state": asdict(state(.2)), "final_state": asdict(state(.3)),
                "outcome":{"termination":"surface_contact"},
                "shared_clock_checkpoint":{"absolute_deadline_s":stop,"unfinished":False,"state_reset":False}},
            "deployment_permission": {"withheld": withheld, "sequencer_state": "skipped" if withheld else "running",
                "payload_released_count": count, "observed_time_s": stop},
            "concurrent_execution": {"schema": "missionos.ship_booster_clock.v1", "tick_order": rows,
                "absolute_deadline_s": stop,
                "maximum_tick_s": max(profile["integration"]["powered_dt_s"], profile["integration"]["coast_dt_s"]),
                "maximum_observed_body_lead_s": 0., "separation_state_preserved": True,
                "observers_run_before_each_body_step": True, "posthoc_ordering": False, "physical_execution": False}}
        return canonical({"schema": "missionos.starship_m3_prefix_study.v1", "scope": prefix.SCOPE,
            "authority": authority, "approval_binding": {"schema": "missionos.starship_m3_prefix_approval_binding.v1",
                "mode": "fixture", "outer_authority_sha256": digest(authority), "m2_authority_sha256": authority["m2_authority_sha256"],
                "ascent_authority_sha256": authority["ascent_authority_sha256"], "human_approval_authenticated": False},
            "profile": profile, "prefix_run": run, "ascent": ascent, "ascent_witnesses": witnesses,
            "trace": trace, "handoff": handoff, "handoff_error": None,
            "source_sha256": {"src/runtime/example.py": "0"*64}, "model_calls": 0, "forecasts": 0,
            "ship_return_dispatched": False, "mission_completed": False, "m3_completed": False, "physical_execution": False})
    return make


@pytest.mark.parametrize("withheld", [False, True])
@pytest.mark.parametrize("case", prefix.CASES)
def test_complete_record_chain_has_narrow_claim(make_study, withheld, case):
    result = verify(make_study(withheld, case))
    assert result["passed"], result
    assert result["handoff_ready"]
    assert not any(result[k] for k in ("ship_return_verified", "booster_recovery_verified", "m3_completed", "physical_execution"))


@pytest.mark.parametrize("mutation", [
    lambda s: s["approval_binding"].update(outer_authority_sha256="0"*64),
    lambda s: s["authority"].update(m2_authority_sha256="0"*64),
    lambda s: s["authority"]["ascent_authority"].update(maximum_requests=99),
    lambda s: s.update(m3_completed=True),
    lambda s: s.update(forecasts=1),
    lambda s: s["prefix_run"]["concurrent_execution"].update(posthoc_ordering=True),
    lambda s: s["prefix_run"]["concurrent_execution"]["tick_order"][1].update(sequence=0),
    lambda s: s["prefix_run"]["concurrent_execution"]["tick_order"][1].update(time_s=-1.),
    lambda s: s["prefix_run"]["concurrent_execution"]["tick_order"][1].update(next_time_s=200.),
    lambda s: s["prefix_run"]["concurrent_execution"].update(maximum_observed_body_lead_s=.1),
    lambda s: s["prefix_run"]["booster_run"]["booster_separation_state"].update(propellant_kg=1.),
    lambda s: s["prefix_run"]["booster_run"].update(development_coast_from_separation={}),
    lambda s: s["ascent_witnesses"][0]["state"].update(propellant_kg=1.),
    lambda s: s["ascent"]["decisions"][0]["request"].update(authority_sha256="0"*64),
    lambda s: s["ascent"]["decisions"][0].update(time_s=0.),
    lambda s: s["ascent"]["decisions"][0].update(physical_engine_command=True),
    lambda s: s["ascent"]["decisions"][0]["response"].update(model_inference_invoked=True),
    lambda s: s["handoff"]["execution_state"].update(propellant_kg=1.),
    lambda s: s["prefix_run"]["final_vehicle"].update(dry_mass_kg=1.),
    lambda s: s["source_sha256"].update({"/private/local/path": "0"*64}),
])
def test_corruption_rejected(make_study, mutation):
    study = make_study()
    mutation(study)
    assert verify(study)["passed"] is False


@pytest.mark.parametrize("mutation", [
    lambda s: s["ascent"].update(confirmations=[]),
    lambda s: s["prefix_run"]["events"].pop(0),
    lambda s: s["prefix_run"]["deployment_permission"].update(withheld=False),
    lambda s: s["prefix_run"]["deployment_permission"].update(sequencer_state="running"),
    lambda s: s["ascent"]["confirmations"][0]["later_observation"].update(time_s=.1),
    lambda s: s["prefix_run"]["events"].append({"event":"payload_released", "time_s":.2}),
])
def test_withhold_needs_actual_effect_and_later_confirmation(make_study, mutation):
    study = make_study(True)
    mutation(study)
    assert verify(study)["passed"] is False


def test_reforged_inventory_cannot_reset_exact_state(make_study):
    study = make_study()
    study["handoff"]["execution_state"]["propellant_kg"] += 1
    study["handoff"]["handoff_sha256"] = digest({k:v for k,v in study["handoff"].items() if k != "handoff_sha256"})
    assert "exact_continuation_state" in verify(study)["issues"]


def test_missing_handoff_is_preserved_not_completion(make_study):
    study = make_study(True)
    study.update(handoff=None, handoff_error="deployment_observation_or_confirmation_missing")
    result = verify(study)
    assert result["passed"] and result["handoff_ready"] is False and result["m3_completed"] is False


def test_independent_health_interlock_does_not_claim_model_decision(make_study):
    study = make_study(True)
    ship = [r for r in study["prefix_run"]["concurrent_execution"]["tick_order"] if r["body_id"] == "ship"]
    for row in ship:
        if row["time_s"] > 0:
            row.update(deployment_withheld=True, sequencer_state="skipped")
    initial, later = observed(ship[0], healthy=False), observed(ship[1], healthy=False)
    study["ascent"].update(decisions=[], requests=0, observations=[initial],
        trigger_events=[{"time_s":0.,"trigger":"observed_engine_loss_interlock","model_request":False,"action":"withhold_deployment"}],
        confirmations=[{"command_time_s":0.,"later_observation":later}])
    study["ascent_witnesses"] = canonical([{"sequence":r["sequence"], "state":asdict(state(r["time_s"],healthy=False)),
        "observation":observed(r,healthy=False)} for r in ship[:2]])
    study["prefix_run"]["events"][0]["time_s"] = 0.
    result = verify(study)
    assert result["passed"], result
    study["ascent"]["trigger_events"][0]["time_s"] = .05
    assert verify(study)["passed"] is False


def test_runner_connects_permission_and_tick_callbacks_without_return(tmp_path, make_study, monkeypatch):
    archived = make_study()
    profile = archived["profile"]
    monkeypatch.setattr(prefix, "sources", lambda: archived["source_sha256"])
    seen = {}
    def simulate(profile, **kwargs):
        seen.update(kwargs)
        assert kwargs["deployment_permission"]() is True
        director = kwargs["mission_director"]
        for entry in archived["trace"]:
            director.tracker.observe(entry["row"])
            director.trace.append(deepcopy(entry))
        return deepcopy(archived["prefix_run"])
    monkeypatch.setattr(prefix, "simulate_concurrent", simulate)
    result = prefix.run_fixture(profile, "normal", tmp_path, splashdown=False)
    assert seen["duration_s"] == prefix.CHECKPOINT_TIME_S
    assert seen["booster_policy"] == seen["development_booster_coast_control"] == "fixed_v1"
    assert callable(seen["on_tick"]) and callable(seen["deployment_permission"])
    assert result["handoff"]["execution_state"] == result["prefix_run"]["final_state"]
    assert result["handoff"]["authority_sha256"] == digest(result["authority"]["m2_authority"])
    assert result["forecasts"] == result["model_calls"] == 0
    assert (tmp_path/"ascent.json").is_file() and (tmp_path/"prefix-run.json").is_file()


def test_unregistered_case_and_changed_profile_rejected(tmp_path):
    with pytest.raises(ValueError, match="not_registered"):
        prefix.m3_prefix_contract("hold_resume")
    with pytest.raises(ValueError, match="registered_profile"):
        prefix.run_fixture({}, "normal", tmp_path)
