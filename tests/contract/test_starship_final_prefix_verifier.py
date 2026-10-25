"""Invented current-state refusal records; no flight, model or control execution."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import socket
import subprocess
from threading import Event

import pytest

from src.runtime import starship_final_prefix_verifier as final
from src.runtime import starship_final_prefix_supervision as supervision
from src.runtime import starship_final_protocol as rpc, starship_final_shared as shared
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission, starship_sixdof_booster as booster
from src.runtime.starship_inflight_runtime import approval_pipe
from src.intelligence.starship_final_supervision_agent import FinalSupervisionAgent
from tests.contract.test_starship_final_prefix_supervision import FixtureTransport, Clock


def native(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(autouse=True)
def no_plant_or_provider(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "Saved refusal checks cannot integrate, forecast, invoke control or providers"
        )

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (env, "step"),
        (mission, "advance_plant"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def refused():
    from tests.contract.test_starship_restored_prefix import setup

    raw, profile, settings, controls, _, sites, goal = setup.__wrapped__()
    settings = shared.ascent_settings(
        shared.read(shared.relative("examples/spaceflight/starship-final/configuration-v1.json"))[
            "ascent_settings"
        ]
    )
    source = shared.source_manifest([shared.PROFILE, *final.contract.CRITICAL_SOURCES])
    context = {"run_id": "refusal-fixture", "mission_id": "refusal-invented", "vehicle_id": "ship"}
    hr, cw = approval_pipe()
    cr, hw = approval_pipe()
    clock = Clock()
    client = rpc.FinalObservationClient(
        cw, cr, plan_sha256="a" * 64, source_sha256=rpc.digest(source), clock=clock
    )
    transport = FixtureTransport(["inhibit_recovery"])
    agent = FinalSupervisionAgent(transport=transport, clock=clock)
    host = rpc.FinalObservationHost(
        hr,
        hw,
        plan_sha256="a" * 64,
        source_sha256=rpc.digest(source),
        handler=agent.assess,
        clock=clock,
    )
    observer = supervision.FinalPrefixSupervision(
        profile,
        run_id=context["run_id"],
        plan_sha256="a" * 64,
        source_sha256=source,
        scope_sha256=rpc.digest({"context": context, "phase": "prefix", "plan": "a" * 64}),
        client=client,
        mode="fixture",
        clock=clock,
    )
    craft = mission.vehicle(profile, "booster")
    engines = tuple(dyn.EngineState(available=i != 32) for i, _ in enumerate(craft.engines))
    state = dyn.State6DOF(
        142.0,
        (env.EARTH_EQUATORIAL_RADIUS_M + 71000.0, 0.0, 0.0),
        (100.0, 1200.0, 0.0),
        dyn.IDENTITY,
        (0.0, 0.001, 0.0),
        260000.0,
        engines,
        tuple(0.0 for _ in craft.aero_panels),
    )
    try:
        observer.observe_booster(state, craft, "booster_boostback_slew")
        for _ in range(1000):
            host.tick()
            observer.observe_booster(state, craft, "booster_boostback_slew")
            if observer.recovery_refused:
                break
            Event().wait(0.001)
        assert observer.recovery_refused
        iterator = booster._simulate_booster_steps(
            profile,
            asdict(state),
            duration_s=1.0,
            guidance_policy="availability_terminal_recovery_v1",
            terminal_recovery_settings=controls,
            return_sites=sites,
            splashdown_goal=goal,
            final_prefix_supervision=observer,
        )
        next(iterator)
        with pytest.raises(StopIteration) as completed:
            next(iterator)
        run = native(completed.value.value)
        run["shared_clock_checkpoint"] = {
            "absolute_deadline_s": 7500.0,
            "unfinished": False,
            "state_reset": False,
        }
        run["runner_booster_peak_witness"] = {
            "state": native(asdict(state)),
            "origin": "all_yielded_physics_ticks_and_final_state; no extra plant step",
            "not_independent_physics_reexecution": True,
        }
        row = observer.record()["requests"][0]
        run["final_supervision_actual_witnesses"] = [
            {
                "schema": "missionos.starship_final_actual_supervision_witness.v1",
                "body": "booster",
                "time_s": state.time_s,
                "phase": "booster_boostback_slew",
                "state": native(asdict(state)),
                "vehicle": native(asdict(craft)),
                "state_sha256": row["observation"]["state_sha256"],
                "vehicle_sha256": row["observation"]["vehicle_sha256"],
                "request_ids": [row["request_id"]],
                "origin": "actual_plant_tick_before_supervision",
                "state_or_inventory_assigned": False,
            }
        ]
        launch = {
            "booster_run": run,
            "booster_separation_state": native(asdict(state)),
            "final_prefix_supervision": native(observer.record()),
            "concurrent_execution": {
                "tick_order": [{"body_id": "booster", "time_s": 142.0, "next_time_s": 142.0}]
            },
        }
        kwargs = {
            "settings": settings,
            "controls": controls.record(),
            "site": sites.divert.to_dict(),
            "goal": goal.to_dict(),
            "horizon": 7500.0,
            "source": source,
            "plan": "a" * 64,
            "context": context,
            "mode": "fixture",
        }
        yield launch, profile, kwargs
    finally:
        for endpoint in (hr, cw, cr, hw):
            endpoint.close()


def test_zero_command_refusal_keeps_actual_state_without_claiming_water_entry(refused):
    launch, profile, kwargs = refused
    before = native(launch)
    final._source_scope(kwargs["source"], kwargs["plan"], kwargs["context"], kwargs["mode"])
    result = final._booster_refusal(launch, profile, **kwargs)
    assert result["passed"] and result["recovery_refused"] and result["recovery_unresolved"]
    assert result["controlled_water_entry_envelope_met"] is False
    assert result["source_authentication_established"] is False
    assert result["host_delivery_requires_separate_rpc_check"] is True
    assert launch == before and launch["booster_run"]["outcome"]["integration_steps"] == 0
    assert launch["booster_run"]["samples"][0]["command"] is None


@pytest.mark.parametrize(
    "change",
    [
        "term",
        "contact",
        "safe",
        "reset",
        "state",
        "fuel",
        "vehicle",
        "availability",
        "phase",
        "target",
        "clock",
        "step",
        "sample_mass",
        "peak",
        "control",
        "scope",
        "action",
        "mode",
        "epoch",
        "witness",
        "borrowed",
        "missing_event",
    ],
)
def test_refusal_snapshot_proposal_and_claim_tampering_is_rejected(refused, change):
    launch, profile, kwargs = refused
    run = launch["booster_run"]
    outcome = run["outcome"]
    if change == "term":
        outcome["termination"] = "surface_contact"
    elif change == "contact":
        run["contact"] = {}
    elif change == "safe":
        outcome["simulated_contact_envelope_met"] = True
    elif change == "reset":
        run["shared_clock_checkpoint"]["state_reset"] = True
    elif change == "state":
        run["final_state"]["propellant_kg"] -= 1.0
    elif change == "fuel":
        run["samples"][0]["propellant_kg"] += 1.0
    elif change == "vehicle":
        run["final_vehicle"]["dry_mass_kg"] += 1.0
    elif change == "availability":
        run["samples"][0]["engine_states"][32]["available"] = True
    elif change == "phase":
        outcome["phase"] = "booster_catch"
    elif change == "target":
        run["active_return_site"]["site_id"] = "capture"
    elif change == "clock":
        run["shared_clock_checkpoint"]["absolute_deadline_s"] = 8000.0
    elif change == "step":
        outcome["integration_steps"] = 1
    elif change == "sample_mass":
        run["samples"][0]["mass_kg"] += 1.0
    elif change == "peak":
        run["runner_booster_peak_witness"]["state"]["time_s"] = 100.0
    elif change == "control":
        run["booster_terminal_recovery"]["control_witnesses"] = [{}]
    elif change == "scope":
        launch["final_prefix_supervision"]["source_sha256"] = "0" * 64
    elif change == "action":
        launch["final_prefix_supervision"]["judgments"][0]["action"] = "splashdown"
    elif change == "mode":
        kwargs["mode"] = "live"
    elif change == "epoch":
        launch["final_prefix_supervision"]["requests"][0]["deadline_s"] += 1.0
    elif change == "witness":
        run["final_supervision_actual_witnesses"] = []
    elif change == "borrowed":
        run["recovery_record"] = {}
    else:
        run["events"] = [row for row in run["events"] if row["event"] != final.REFUSAL]
    with pytest.raises((ValueError, KeyError, TypeError, final.saved._Invalid)):
        final._booster_refusal(launch, profile, **kwargs)


def test_normal_and_unknown_paths_are_delegated_to_the_unchanged_legacy_checker(monkeypatch):
    sent = []
    result = (False, 7, {"termination": "time_limit"}, False, None)

    def old(*args, **kwargs):
        sent.append((args, kwargs))
        return result

    monkeypatch.setattr(final.legacy, "_prefix_physical", old)
    frozen = {
        "settings": object(),
        "controls": {},
        "expected_settings": {},
        "expected_site": {},
        "expected_goal": {},
        "expected_return_sites": {},
        "expected_prefix_horizon_s": 7500.0,
    }
    for terminal in ("surface_contact", "time_limit", "unknown_refusal"):
        launch = {"booster_run": {"outcome": {"termination": terminal}}}
        assert final.verify_prefix_physical(launch, {}, **frozen) is result
        assert sent[-1] == ((launch, {}), frozen)


def test_common_checks_fingerprint_and_external_source_binding_cannot_be_bypassed(refused):
    _, _, kwargs = refused
    source = deepcopy(kwargs["source"])
    source[final.SOURCE] = "0" * 64
    with pytest.raises(ValueError, match="source_binding"):
        final._source_scope(source, kwargs["plan"], kwargs["context"], kwargs["mode"])
    raw = (final.ROOT / "src/runtime/starship_restored_verifier.py").read_text()
    block = raw[
        raw.index("def _prefix_physical(") : raw.index(
            '    booster = launch.get("booster_run")', raw.index("def _prefix_physical(")
        )
    ]
    assert sha256(block.encode()).hexdigest() == final.COPIED_COMMON_SHA256


def test_optin_wrapper_calls_common_checks_then_actual_refusal_without_fake_normal_record(
    refused, monkeypatch
):
    launch, profile, kwargs = refused
    seen = []

    def common(actual, p, **frozen):
        assert actual is launch and p is profile
        seen.append(frozen)
        return True, 11, {"booster_return_invoked": True}, {"stage_separation": [{"time_s": 142.0}]}

    monkeypatch.setattr(final, "_common_prefix", common)

    def legacy_forbidden(*_a, **_k):
        raise AssertionError("Refusal cannot be fabricated into old normal input")

    monkeypatch.setattr(final.legacy, "_prefix_physical", legacy_forbidden)
    result = final.verify_prefix_physical(
        launch,
        profile,
        settings=kwargs["settings"],
        controls=kwargs["controls"],
        expected_settings=kwargs["settings"].record(),
        expected_site=kwargs["site"],
        expected_goal=kwargs["goal"],
        expected_return_sites={},
        expected_prefix_horizon_s=7500.0,
        expected_source_sha256=kwargs["source"],
        expected_plan_sha256=kwargs["plan"],
        expected_context=kwargs["context"],
        expected_mode=kwargs["mode"],
    )
    assert result[0] and result[1] == 11 and result[3] is False and result[4]["recovery_unresolved"]
    assert len(seen) == 1 and seen[0]["settings"] is kwargs["settings"]
